"""名称候选仅供 DeepSeek 复核；字符相似度不直接产生匹配或改写数据。"""
from difflib import SequenceMatcher
import re
import unicodedata


def name_key(name):
    return "".join(unicodedata.normalize("NFKC", name).split()).casefold()


def build_name_candidates(rows, rules, limit=5):
    names = {r["rule_indicator_name"].strip(): r
             for r in rules if isinstance(r.get("rule_indicator_name"), str) and r["rule_indicator_name"].strip()}
    candidates = []
    for row in rows:
        name = row["indicator_name"].strip()
        if name in names:
            continue
        # 每个未命中名称都复核，无相似度通过门槛；完整 GZKJG 同时提供给模型。
        ranked = sorted(names, key=lambda target: (
            -SequenceMatcher(None, name_key(name), name_key(target), autojunk=False).ratio(), target))[:limit]
        candidates.append({"indicator_name": row["indicator_name"], "source": row["source"],
                           "indicator_unit": row.get("indicator_unit", ""),
                           "indicator_release_frequency": row.get("indicator_release_frequency", ""),
                           "candidates": [{"rule_indicator_name": target,
                                           "rule_indicator_code": str(names[target].get("rule_indicator_code", "")),
                                           "rule_constant_name": names[target].get("rule_constant_name", ""),
                                           "rule_unit_ratio": names[target].get("rule_unit_ratio", ""),
                                           "indicator_release_frequency": names[target].get("indicator_release_frequency", ""),
                                           "name_reference": names[target].get("name_reference", []),
                                           "name_similarity": round(SequenceMatcher(
                                               None, name_key(name), name_key(target), autojunk=False).ratio(), 4)}
                                          for target in ranked]})
    return candidates


def review_rules(reference_rules, gzk):
    """保留拼接规则证据，同时允许DeepSeek选择GZK最终名称；不自动判同义。"""
    names = {r["rule_indicator_name"].strip(): dict(r) for r in reference_rules}
    for rule in gzk:
        name = rule.get("rule_indicator_name", "").strip()
        if name:
            names[name] = {**names.get(name, {}), **rule}
    return list(names.values())


def build_review_candidates(rows, reference_rules, gzk, workbook=None):
    concat = build_name_candidates(rows, reference_rules) if reference_rules else []
    final = build_name_candidates(rows, gzk)
    key = lambda item: (item["indicator_name"], item["source"]["sheet"], item["source"]["cell"])
    concat_index, final_index = {key(c): c for c in concat}, {key(c): c for c in final}
    combined = build_name_candidates(rows, [])
    result = []
    source_sheets = {s['name']: s['rows'] for s in (workbook or {}).get('sheets', [])}
    for row in combined:
        identity = key(row)
        if identity not in concat_index and identity not in final_index:
            continue
        first = concat_index.get(identity, {}).get("candidates", [])
        second = final_index.get(identity, {}).get("candidates", [])
        merged = {}
        for candidate in first + second:
            name = candidate["rule_indicator_name"]
            merged[name] = {**merged.get(name, {}), **candidate,
                            "name_reference": merged.get(name, {}).get("name_reference", []) or candidate.get("name_reference", [])}
        entry = {**row, "candidates": list(merged.values()), "concatenation_candidates": first,
                       "gzk_candidates": second, "exact_concat_match": bool(reference_rules) and identity not in concat_index,
                       "exact_gzk_match": identity not in final_index}
        cell = re.fullmatch(r'[A-Z]+([1-9][0-9]*)', row['source']['cell'])
        sheet = source_sheets.get(row['source']['sheet'])
        if cell and sheet is not None:
            number = int(cell[1])
            entry['source_evidence'] = {'sheet': row['source']['sheet'], 'cell': row['source']['cell'],
                'nearby_rows': [{'row': n + 1, 'cells': sheet[n]} for n in range(max(0, number - 13), min(len(sheet), number + 1))],
                'full_sheet_available_in_workbook': True}
        result.append(entry)
    return {"concatenation_precheck": concat, "gzk_unmatched": final, "review_candidates": result}


def required_alignments(history, gzk=None):
    """同一任务已确认的同义标准名称必须通过代码重建落实，不能在后轮静默丢弃。"""
    targets = {}
    gzk_names = {r["rule_indicator_name"].strip() for r in gzk or [] if isinstance(r.get("rule_indicator_name"), str)}
    for attempt in history:
        review = attempt.get("evaluation", {}).get("name_review", {})
        for item in review.get("candidate_reviews", []):
            if item["decision"] in {"same_indicator", "already_canonical"}:
                key = (item["source"]["sheet"], item["source"]["cell"])
                target = item["target_indicator_name"]
                if key in targets and targets[key]["target_indicator_name"] != target:
                    previous = targets[key]["target_indicator_name"]
                    if previous in gzk_names and target not in gzk_names:
                        continue
                    if previous in gzk_names or target not in gzk_names:
                        raise ValueError("同一源单元格的完整标准名称判断冲突，需核对拼接规则与名称口径")
                targets[key] = {"source": item["source"], "target_indicator_name": target,
                                "reason": item["reason"], "target_basis": "GZKJG" if target in gzk_names else "concatenation"}
    return list(targets.values())


def negative_review_candidates(review, candidates, gzk):
    """仅召回可能因省略/增加限定词而误判不同的名称，不自行认定同义。"""
    key = lambda item: (item['indicator_name'], item['source']['sheet'], item['source']['cell'])
    negatives = {key(r) for r in review.get('candidate_reviews', []) if r['decision'] == 'different_indicator'}
    standards = [name_key(r['rule_indicator_name']) for r in gzk]
    return [c for c in candidates if key(c) in negatives and any(
        name_key(c['indicator_name']) and target and name_key(c['indicator_name']) != target
        and (name_key(c['indicator_name']) in target or target in name_key(c['indicator_name'])) for target in standards)]

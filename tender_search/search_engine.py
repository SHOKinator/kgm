"""TF-IDF + морфология + числовые признаки + предсказание цены."""

from __future__ import annotations

import math
import statistics
from typing import Optional

from .config import WEIGHT_PRED_ANCHOR_COUNT, WEIGHT_PRED_R2, WEIGHT_PRED_CONSISTENCY
from .models import Product, SearchResult, PricePrediction
from .optional_deps import HAS_NUMPY, HAS_SKLEARN, np, TfidfVectorizer, _sk_cos
from .text_features import CRITICAL_UNITS, NumFeat, extract_features, get_root_word, jaccard, lemmatize


class SearchEngine:
    """TF-IDF + морфология + числовые признаки + предсказание цены."""

    def __init__(self, products: list[Product]):
        self.products = products
        self._lemmas: list[str] = []
        self._feats: list[list[NumFeat]] = []
        self._roots: list[str] = []
        for p in products:
            self._lemmas.append(lemmatize(p.full_text))
            self._feats.append(extract_features(p.full_text))
            self._roots.append(get_root_word(p.name))
        self._tfidf: Optional["TfidfVectorizer"] = None
        self._matrix = None
        if HAS_SKLEARN and len(products) >= 2:
            try:
                self._tfidf = TfidfVectorizer(
                    analyzer="word", min_df=1, sublinear_tf=True,
                    ngram_range=(1, 2),
                )
                self._matrix = self._tfidf.fit_transform(self._lemmas)
            except Exception:
                self._tfidf = None

    def _text_sim(self, q_lemma: str, idx: int) -> float:
        if self._tfidf is not None and self._matrix is not None:
            q_vec = self._tfidf.transform([q_lemma])
            score = float(_sk_cos(q_vec, self._matrix[idx])[0][0])
            jac = jaccard(q_lemma, self._lemmas[idx])
            return 0.7 * score + 0.3 * jac
        return jaccard(q_lemma, self._lemmas[idx])

    def _numeric_score(self, q_feats: list[NumFeat], p_idx: int):
        if not q_feats:
            return 1.0, "", 1.0
        p_feats = self._feats[p_idx]
        scores, details, critical_scores = [], [], []
        for qf in q_feats:
            cand = [pf for pf in p_feats if pf.unit == qf.unit]
            if not cand:
                sc = 0.2 if qf.unit in CRITICAL_UNITS else 0.5
                scores.append(sc)
                if qf.unit in CRITICAL_UNITS:
                    critical_scores.append(sc)
                details.append(f"нет {qf.unit}")
                continue
            best = max(cand, key=lambda pf: pf.match_score(qf.value))
            sc = best.match_score(qf.value)
            scores.append(sc)
            if qf.unit in CRITICAL_UNITS:
                critical_scores.append(sc)
            if sc >= 0.99:
                if best.is_range:
                    details.append(f"{qf.value} {qf.unit} ∈ [{best.lo}–{best.hi}]✓")
                else:
                    details.append(f"{qf.value} {qf.unit}✓")
            elif sc >= 0.5:
                ref = f"[{best.lo}–{best.hi}]" if best.is_range else str(best.value)
                details.append(f"{qf.value}≈{ref} {qf.unit}")
            else:
                ref = f"[{best.lo}–{best.hi}]" if best.is_range else str(best.value)
                details.append(f"{qf.value}✗{ref} {qf.unit}")
        log_sum = sum(math.log(max(s, 1e-6)) for s in scores)
        geo = math.exp(log_sum / len(scores))
        crit_min = min(critical_scores, default=1.0)
        if crit_min < 0.25:
            geo *= 0.4
        return geo, ", ".join(details), crit_min

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        if not self.products:
            return []
        q_lemma = lemmatize(query)
        q_feats = extract_features(query)
        q_root = get_root_word(query)

        scored = []
        for i, _ in enumerate(self.products):
            text_sc = self._text_sim(q_lemma, i)
            num_sc, detail, crit_min = self._numeric_score(q_feats, i)
            final = (0.35 * text_sc + 0.65 * num_sc) if q_feats else text_sc
            if q_root and self._roots[i] == q_root:
                final = min(1.0, final + 0.03)
            scored.append((i, final, text_sc, num_sc, detail, crit_min))

        scored.sort(key=lambda x: -x[1])
        results: list[SearchResult] = []
        seen: set = set()
        has_nums = bool(q_feats)

        for i, final, text_sc, num_sc, detail, crit_min in scored:
            if len(results) >= top_k:
                break
            if i in seen or final < 0.08:
                continue
            same_root = q_root and (self._roots[i] == q_root)
            if not has_nums:
                if text_sc >= 0.28 and same_root:
                    mtype = "precise"
                elif text_sc >= 0.18 and same_root:
                    mtype = "range"
                elif text_sc >= 0.10 and same_root:
                    mtype = "close"
                else:
                    continue
            else:
                text_ok = (text_sc >= 0.05 and same_root) or text_sc >= 0.22
                if crit_min >= 0.99 and num_sc >= 0.85 and text_ok:
                    mtype = "precise"
                elif num_sc >= 0.60 and (text_sc >= 0.12 or same_root):
                    mtype = "range"
                elif num_sc >= 0.30 and (text_sc >= 0.15 or same_root):
                    mtype = "close"
                else:
                    continue
            results.append(SearchResult(
                product=self.products[i], score=final, match_type=mtype,
                num_score=num_sc, text_score=text_sc, explanation=detail,
            ))
            seen.add(i)

        if len(results) < top_k and q_root:
            for i, final, text_sc, num_sc, detail, crit_min in scored:
                if len(results) >= top_k:
                    break
                if i in seen:
                    continue
                if self._roots[i] == q_root:
                    results.append(SearchResult(
                        product=self.products[i], score=final, match_type="category",
                        num_score=num_sc, text_score=text_sc,
                        explanation=f"Та же категория: {q_root}",
                    ))
                    seen.add(i)
        return results

    def predict_price(self, query: str) -> Optional[PricePrediction]:
        """Линейная регрессия по характеристикам товаров той же категории."""
        q_feats = extract_features(query)
        q_root = get_root_word(query)
        q_lemma = lemmatize(query)
        if not q_feats or not q_root:
            return None

        TEXT_SIM_THRESHOLD = 0.12
        anchors = [(self.products[i], self._feats[i])
                   for i in range(len(self.products))
                   if self._roots[i] == q_root and self.products[i].price > 0
                   and self._text_sim(q_lemma, i) >= TEXT_SIM_THRESHOLD]
        if len(anchors) < 2:
            anchors = [(self.products[i], self._feats[i])
                       for i in range(len(self.products))
                       if self._roots[i] == q_root and self.products[i].price > 0]
        if len(anchors) < 2:
            return None

        best_result: Optional[PricePrediction] = None
        best_r2 = -999.0
        for qf in q_feats:
            if qf.unit not in CRITICAL_UNITS:
                continue
            points = []
            for p, pfeats in anchors:
                pf_list = [f for f in pfeats if f.unit == qf.unit]
                if not pf_list:
                    continue
                pf = pf_list[0]
                x = qf.value if (pf.is_range and pf.lo <= qf.value <= pf.hi) else pf.value
                points.append((x, p.price))
            if len(points) < 2:
                continue
            if len(points) >= 3 and HAS_NUMPY:
                prices = sorted(pt[1] for pt in points)
                median = prices[len(prices) // 2]
                points = [pt for pt in points if median / 10 <= pt[1] <= median * 10]
            if len(points) < 2:
                continue
            points.sort(key=lambda x: x[0])
            xs = [pt[0] for pt in points]
            ys = [pt[1] for pt in points]
            if HAS_NUMPY:
                xs_np = np.array(xs, dtype=float); ys_np = np.array(ys, dtype=float)
                n, sx, sy = len(xs_np), xs_np.sum(), ys_np.sum()
                sxx = (xs_np ** 2).sum(); sxy = (xs_np * ys_np).sum()
                denom = n * sxx - sx ** 2
                if abs(denom) < 1e-12:
                    continue
                a = (n * sxy - sx * sy) / denom
                b = (sy - a * sx) / n
                pred = a * qf.value + b
                y_mean = ys_np.mean(); ss_tot = ((ys_np - y_mean) ** 2).sum()
                ss_res = ((ys_np - (a * xs_np + b)) ** 2).sum()
                r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
            else:
                x1, y1 = points[0]; x2, y2 = points[-1]
                if abs(x2 - x1) < 1e-12:
                    continue
                a = (y2 - y1) / (x2 - x1); b = y1 - a * x1
                pred = a * qf.value + b
                r2 = 0.5
            if r2 > best_r2 and pred > 0:
                best_r2 = r2
                pts_str = ", ".join(f"{x} {qf.unit}→{y:,.0f}₸" for x, y in points[:4])
                expl = (f"Регрессия по {len(points)} товарам категории "
                        f"«{q_root}» (R²={r2:.2f}) | {pts_str}")
                if a > 0:
                    expl += f" | тренд: +{a:,.0f}₸/{qf.unit}"
                confidence = _prediction_confidence(points, r2)
                best_result = PricePrediction(
                    price=max(0.0, pred), explanation=expl,
                    confidence_pct=confidence, r2=r2, n_anchors=len(points),
                )
        return best_result


def _prediction_confidence(points: list[tuple[float, float]], r2: float) -> float:
    """Confidence-score прогноза: доля от числа точек-якорей, качества регрессии (R²)
    и согласованности цен среди якорей. Веса и формула перенесены/адаптированы из
    tender_claude_code/price_engine.py (там был LLM/URL-сигнал — здесь его нет,
    поэтому вес перераспределён между этими тремя факторами)."""
    source_factor = min(len(points) / 5.0, 1.0)
    r2_factor = max(0.0, min(r2, 1.0))
    prices = [y for _, y in points]
    if len(prices) > 1:
        mean_p = statistics.mean(prices)
        cv = statistics.stdev(prices) / mean_p if mean_p > 0 else 1.0
        consistency = max(0.0, 1.0 - min(cv, 1.0))
    else:
        consistency = 0.5
    confidence = (
        source_factor * WEIGHT_PRED_ANCHOR_COUNT +
        r2_factor * WEIGHT_PRED_R2 +
        consistency * WEIGHT_PRED_CONSISTENCY
    )
    return round(confidence * 100, 1)

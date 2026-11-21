"""Review sentiment with VADER (tiny, rule-based, no model download).

A review that praises one part and slams another used to be labelled by its overall
score, so one strongly negative half could outweigh the positive half. We now also
score each clause (split on sentence ends and words like "but"/"however") and call a
review "mixed" when it has a real positive side AND a real negative side."""
import re

_analyzer = None
_SPLIT = re.compile(r'(?<=[.!?;])\s+|\n+|\s*\b(?:but|however|although|though|yet|except)\b\s*', re.I)
CLAUSE = 0.3     # a clause counts as positive/negative beyond this
OVERALL = 0.25   # overall score needed to call a one-sided review positive/negative


def _clauses(text):
    parts = (p.strip(' ,.;-') for p in _SPLIT.split(text) if p)
    return [p for p in parts if p]


def analyze(text):
    global _analyzer
    if not text or not text.strip():
        return None
    if _analyzer is None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        except ImportError:
            return None
        _analyzer = SentimentIntensityAnalyzer()
    overall = _analyzer.polarity_scores(text)['compound']
    scores = [_analyzer.polarity_scores(c)['compound'] for c in _clauses(text)]
    pos = sum(1 for s in scores if s >= CLAUSE)
    neg = sum(1 for s in scores if s <= -CLAUSE)
    if pos and neg and min(pos, neg) * 2 >= max(pos, neg):
        label = 'mixed'                                   # both sides, reasonably balanced
    elif overall >= OVERALL:
        label = 'positive'
    elif overall <= -OVERALL:
        label = 'negative'
    else:
        label = 'mixed'
    return {'score': round(overall, 3), 'label': label}


def summarize(texts):
    results = [r for r in (analyze(t) for t in texts) if r]
    if not results:
        return None
    counts = {'positive': 0, 'mixed': 0, 'negative': 0}
    for r in results:
        counts[r['label']] += 1
    avg = sum(r['score'] for r in results) / len(results)
    overall = 'positive' if avg >= OVERALL else 'negative' if avg <= -OVERALL else 'mixed'
    return {'overall': overall, 'average': round(avg, 3), 'counts': counts, 'reviews': len(results)}

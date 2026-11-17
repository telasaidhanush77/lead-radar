"""Paid list prices, used only to say what each provider WOULD cost per 1k leads.
We run on free tiers. Prices change: re-check the source before quoting a number."""

PRICES = {
    "gemini": {
        "model": "gemini-3.8-flash",
        "in_per_m": 0.75,    # USD per 1M input tokens (rises to 1.50 on 2027-01-01)
        "out_per_m": 3.75,   # USD per 1M output tokens, thinking tokens included
        "source": "https://aistudio.google.com/docs/pricing",
        "checked": "2026-11-17",
    },
    "groq": {
        "model": "openai/gpt-oss-120b",
        "in_per_m": 0.15,
        "out_per_m": 0.60,
        "source": "https://console.groq.com/docs/models",
        "checked": "2026-11-17",
    },
}


def cost_per_1k(in_per_m: float, out_per_m: float, avg_in: float, avg_out: float) -> float:
    """USD to classify 1,000 leads, from average tokens per call."""
    return (avg_in * in_per_m + avg_out * out_per_m) / 1_000_000 * 1000

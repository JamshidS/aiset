"""Dated USD Standard API reference, never a ChatGPT subscription bill.

Sources: https://developers.openai.com/api/docs/models/{model}
Unknown models return None. These estimates use the configured session model;
rollout telemetry does not prove the model billed for each response. No lookup
or network request occurs when the dashboard is rendered.
"""

RATES = {  # input, cached input, output per million, 2026-10-31
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6.1-terra": (1.00, 0.05, 5.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
}


def estimate(model, input_tokens, cache_write, cached_input, output):
    rates = RATES.get(model)
    if rates is None or cache_write:
        return None  # cache retention/service tier isn't present in rollouts
    fresh, cache, generated = rates
    if input_tokens > 272000:
        fresh, cache, generated = fresh * 2, cache * 2, generated * 1.5
    return ((input_tokens - cached_input) * fresh + cached_input * cache + output * generated) / 1000000

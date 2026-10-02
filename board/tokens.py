"""API list prices and the token and cost calculation (TokenMeter). Claude and Codex use the same meter."""

import functools
import re


# API list prices (USD per million tokens): base input, 5-minute cache write, 1-hour cache write, cache read, output.
# Source: platform.claude.com/docs/en/about-claude/pricing (checked 2026-09-29). Models from 4.6 on have no surcharge on the 1M context.
PRICES = {
    'claude-fable-5-1': (10, 12.5, 20, 0.25, 50),
    'claude-mythos-5-1': (10, 12.5, 20, 0.25, 50),
    'claude-fable-5': (10, 12.5, 20, 1, 50),
    'claude-mythos-5': (10, 12.5, 20, 1, 50),
    'claude-opus-5-5': (4, 5, 8, 0.20, 20),
    'claude-opus-5': (5, 6.25, 10, 0.50, 25),
    'claude-opus-4-8': (5, 6.25, 10, 0.50, 25),
    'claude-opus-4-7': (5, 6.25, 10, 0.50, 25),
    'claude-opus-4-6': (5, 6.25, 10, 0.50, 25),
    'claude-opus-4-5': (5, 6.25, 10, 0.50, 25),
    'claude-sonnet-5-5': (2, 2.5, 4, 0.20, 10),
    'claude-sonnet-5': (2, 2.5, 4, 0.20, 10),
    'claude-sonnet-4-6': (3, 3.75, 6, 0.30, 15),
    'claude-sonnet-4-5': (3, 3.75, 6, 0.30, 15),
    'claude-haiku-4-5': (1, 1.25, 2, 0.10, 5),
    # Codex (OpenAI API list price, Standard, up to 272K). Source: developers.openai.com/api/docs/models/gpt-6-astra,
    # developers.openai.com/api/docs/pricing (checked 2026-09-30). Cache write is a single rate, so it is set the same in the 5-minute and 1-hour columns.
    # The Codex window (258.4K) is smaller than 272K, so there is no long-context surcharge. codex-auto-review (guardian) has no unit price.
    'gpt-6-astra': (10, 12.5, 12.5, 1, 50),
    'gpt-6.1-sol': (2, 2.5, 2.5, 0.10, 10),
}
PRICE_KEYS = sorted(PRICES, key=len, reverse=True)


@functools.lru_cache(maxsize=256)
def price_of(model):
    m = (model or '').replace('[1m]', '')
    return next((PRICES[k] for k in PRICE_KEYS if m.startswith(k)), None)


def call_cost(it, model):
    """The cost of one call (a usage or an iterations item), itemised. None if the price is unknown."""
    p = price_of(model)
    return _itemised(it, p) if p else None


def _itemised(it, p):
    cc = it.get('cache_creation') or {}
    w5, w1 = cc.get('ephemeral_5m_input_tokens'), cc.get('ephemeral_1h_input_tokens')
    if w5 is None and w1 is None:
        w5, w1 = it.get('cache_creation_input_tokens') or 0, 0
    mult = (2 if it.get('speed') == 'fast' else 1) * (1.1 if it.get('inference_geo') == 'us' else 1)
    return {'cost_input': (it.get('input_tokens') or 0) * p[0] * mult / 1e6,
            'cost_write': ((w5 or 0) * p[1] + (w1 or 0) * p[2]) * mult / 1e6,
            'cost_read': (it.get('cache_read_input_tokens') or 0) * p[3] * mult / 1e6,
            'cost_output': (it.get('output_tokens') or 0) * p[4] * mult / 1e6}


def claude_model_short(model):
    """Short name of a Claude model: claude-opus-5-5[1m] → opus5.5, claude-haiku-4-5-20251001 → haiku4.5. claude if unknown."""
    m = short_model(model)
    k = re.match(r'^([a-z]+)-(\d+)(?:-(\d+))?$', m)
    if not k:
        return m or 'claude'
    return k.group(1) + k.group(2) + ('.' + k.group(3) if k.group(3) else '')


def cx_model_short(model):
    """Short name of a Codex model: gpt-6.1-sol → sol6.1, gpt-6-astra → astra6. codex if unknown."""
    m = re.sub(r'^gpt-', '', (model or '').strip().lower())
    k = re.match(r'^(\d+(?:\.\d+)?)-([a-z]+)$', m) or re.match(r'^([a-z]+)-(\d+(?:\.\d+)?)$', m)
    if not k:
        return (model or '').strip() or 'codex'
    ver, name = (k.group(1), k.group(2)) if k.group(1)[0].isdigit() else (k.group(2), k.group(1))
    return name + ver


@functools.lru_cache(maxsize=256)
def short_model(model):
    m = re.sub(r'-\d{8}$', '', (model or '').replace('[1m]', ''))
    return m.replace('claude-', '')


ADV_SUFFIX = ' 조언'      # old `models` key suffix of an advisor call (Korean, kept for open pages); `model_costs` says the same as a flag


class TokenMeter:
    """Counts API call usage once per response (message.id).

    - A response is stored as one line per content block, each with its usage. In a sub-agent record the earlier lines'
      output_tokens is an in-stream intermediate value and the last line is the final one. So when a new line with the same id arrives,
      the earlier value is subtracted and replaced by the new one.
    - If a response calls an advisor model, it is recorded apart as 'advisor_message' in usage.iterations and is not in the top-level total.
    - The current context is the tokens that went into the last call (new input + cache read + cache write).
    """

    KEYS = ('input', 'cache_write', 'cache_read', 'output', 'calls', 'adv_input', 'adv_output', 'adv_calls',
            'cost', 'cost_input', 'cost_write', 'cost_read', 'cost_output', 'adv_cost', 'unpriced', 'reasoning',
            'g_calls', 'g_input', 'g_cache_read', 'g_output')
    G_KEYS = {'g_calls': 'calls', 'g_input': 'input', 'g_cache_read': 'cache_read', 'g_output': 'output'}

    def __init__(self):
        self.by_msg = {}
        self.ctx = 0
        self.ctx_limit = 200000
        self.fixed_limit = False  # Codex: the window size is in the record, so the "over 200k means 1M" rule is not used
        self.t = dict.fromkeys(self.KEYS, 0)
        self.models = {}          # short model name -> cost

    def model(self, model_id):
        if '[1m]' in (model_id or ''):
            self.ctx_limit = 1000000

    def add(self, message):
        u = message.get('usage') or {}
        if not u:
            return
        if not u.get('iterations'):
            return self._add_one(message, u)
        its = u['iterations']
        calls = [it for it in its if it.get('type', 'message') != 'advisor_message']
        if calls:
            ctx = sum(calls[-1].get(k) or 0 for k in
                      ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
            if ctx:
                self.ctx = ctx
                if ctx > self.ctx_limit and not self.fixed_limit:   # even without a [1m] tag on the model, over 200k means a 1M window
                    self.ctx_limit = 1000000
        c = dict.fromkeys(self.KEYS, 0)
        cm = {}
        for it in its:
            adv = it.get('type') == 'advisor_message'
            model = it.get('model') if adv else message.get('model')
            if not it.get('speed') and not it.get('inference_geo'):
                it = dict(it, speed=u.get('speed'), inference_geo=u.get('inference_geo'))
            if adv:
                c['adv_input'] += sum(it.get(k) or 0 for k in
                                      ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
                c['adv_output'] += it.get('output_tokens') or 0
                c['adv_calls'] += 1
            else:
                c['input'] += it.get('input_tokens') or 0
                c['cache_write'] += it.get('cache_creation_input_tokens') or 0
                c['cache_read'] += it.get('cache_read_input_tokens') or 0
                c['output'] += it.get('output_tokens') or 0
                c['calls'] += 1
            if model and model.startswith('<'):
                continue          # '<synthetic>': not an API call
            k = call_cost(it, model)
            if k is None:
                c['unpriced'] += 1
                continue
            total = sum(k.values())
            c['cost'] += total
            if adv:
                c['adv_cost'] += total
            for kk, v in k.items():
                c[kk] += v
            key = short_model(model) + (ADV_SUFFIX if adv else '')
            cm[key] = cm.get(key, 0) + total
        self._apply(message.get('id') or id(message), c, cm)

    def _add_one(self, message, u):
        """`add` for the usual response, one call and no `iterations`: the same counts as the general path, worked out without copying the usage or building
        the full key set (only the keys that are not zero are kept; _apply takes a missing key for 0)."""
        inp, cw, cr, out = (u.get('input_tokens') or 0, u.get('cache_creation_input_tokens') or 0, u.get('cache_read_input_tokens') or 0, u.get('output_tokens') or 0)
        ctx = inp + cr + cw
        if ctx:
            self.ctx = ctx
            if ctx > self.ctx_limit and not self.fixed_limit:   # even without a [1m] tag on the model, over 200k means a 1M window
                self.ctx_limit = 1000000
        c, cm = {'input': inp, 'cache_write': cw, 'cache_read': cr, 'output': out, 'calls': 1}, {}
        model = message.get('model')
        if not (model and model.startswith('<')):          # '<synthetic>': not an API call
            p = price_of(model)
            k = _itemised(u, p) if p else None
            if k is None:
                c['unpriced'] = 1
            else:
                total = sum(k.values())
                c['cost'] = total
                c.update(k)
                cm[short_model(model)] = total
        self._apply(message.get('id') or id(message), c, cm)

    def _apply(self, mid, c, cm):
        """If the same id comes again, subtract the earlier value and replace it with the new one. A key a count does not hold is 0."""
        old = self.by_msg.get(mid)
        t = self.t
        if old:
            oc = old[0]
            for k in (c.keys() | oc.keys()):
                t[k] += c.get(k, 0) - oc.get(k, 0)
            for k, v in old[1].items():
                self.models[k] -= v
        else:
            for k, v in c.items():
                t[k] += v
        for k, v in cm.items():
            self.models[k] = self.models.get(k, 0) + v
        self.by_msg[mid] = (c, cm)

    def add_codex(self, rid, u, model, ctx=True, guardian=False, calls=1):
        """Converts Codex usage (input includes cache reads) to the same meaning as Claude's and counts it.
        With guardian=True it adds to the parent total and also records into the guardian subset. With ctx=False it leaves the current context alone."""
        u = u or {}
        cached = u.get('cached_input_tokens') or 0
        cw = u.get('cache_write_input_tokens') or 0
        inp = max(0, (u.get('input_tokens') or 0) - cached - cw)
        out = u.get('output_tokens') or 0
        c = dict.fromkeys(self.KEYS, 0)
        c.update(input=inp, cache_read=cached, cache_write=cw, output=out, calls=calls,
                 reasoning=u.get('reasoning_output_tokens') or 0)
        if guardian:
            c.update(g_calls=calls, g_input=inp, g_cache_read=cached, g_output=out)
        cm = {}
        k = call_cost({'input_tokens': inp, 'cache_creation_input_tokens': cw, 'cache_read_input_tokens': cached,
                       'output_tokens': out}, model)
        if k is None:
            c['unpriced'] = calls
        else:
            c['cost'] = sum(k.values())
            c.update(k)
            if c['cost']:
                cm[short_model(model)] = c['cost']
        if ctx and u.get('input_tokens'):
            self.ctx = u['input_tokens']
        self._apply(rid, c, cm)

    def model_costs(self):
        """[{model, advisor, cost}]: `models` without the suffix parsing; an advisor call's cost is listed apart from the same model's own."""
        out = []
        for k, v in self.models.items():
            if v > 1e-9:
                adv = k.endswith(ADV_SUFFIX)
                out.append({'model': k[:-len(ADV_SUFFIX)] if adv else k, 'advisor': adv, 'cost': v})
        return out

    def as_dict(self):
        d = dict(self.t, ctx=self.ctx, ctx_limit=self.ctx_limit,
                 models={k: v for k, v in self.models.items() if v > 1e-9}, model_costs=self.model_costs())
        g = {v: d.pop(k) for k, v in self.G_KEYS.items()}
        if g['calls'] or g['input'] or g['output']:
            d['guardian'] = g
        return d

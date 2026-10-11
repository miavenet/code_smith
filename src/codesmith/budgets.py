"""Budget reservations live in agent intents; settlement is one durable state transition."""

import datetime
import time


class Paused(Exception):
    """The owner asked for a pause (`runner pause`); raised only where a call is about to start,
    so the run stops with the same bookkeeping as a budget stop and nothing in flight is lost."""


class Exhausted(Exception):
    """No call may start: the dollar budget (`hint` --add-budget) or, for agents that report no
    cost, the token cap (`hint` --add-tokens) is used up."""

    def __init__(self, message, hint="--add-budget USD"):
        super().__init__(message)
        self.hint = hint


def cap_for(agent, task):
    return float(task['budget_usd']) if agent.reports_cost else 0.0


def tokens_used(state):
    """Measured unpriced tokens, plus the tokens held for calls whose usage stayed unknown under
    the cap (BUD-12). Runs recorded before BUD-12 hold none."""
    return tokens_measured(state) + tokens_held(state)


def tokens_measured(state):
    unpriced = state['spend']['unpriced']
    return unpriced['tokens_in'] + unpriced['tokens_out']


def cached_share(unpriced):
    """The percentage of measured unpriced input tokens the provider read from its cache, or None
    when no call reported a cache read (and in a run recorded before they were counted)."""
    cached, tokens_in = int(unpriced.get('cached_tokens_in', 0)), int(unpriced.get('tokens_in', 0))
    return round(100 * cached / tokens_in) if cached and tokens_in else None


def tokens_held(state):
    """Tokens held for unknown usage (`counted_tokens`): never measured, so never shown or added
    as measured usage; they count only against the stop line (BUD-16)."""
    return int(state['spend']['unpriced'].get('counted_tokens', 0))


def token_cap(state):
    """0 means no cap: runs recorded before the cap existed have none."""
    return int(state.get('run_budget_tokens', 0) or 0)


def fits_tokens(state, agent):
    """An agent that reports no dollar cost may start a call only while the run's unpriced usage
    is under `run_budget_tokens`. Usage arrives when a call ends, so the cap is a stop line, not a
    ceiling: the call that crosses it completes. A call that ends without its final event and
    whose usage stays unknown counts what was left under the line when it started
    (`token_remainder`), so it reaches the line and the owner decides with `--add-tokens`
    (BUD-12, BUD-20); one that completed without reporting usage (a `command` agent) counts its
    bounded `token_reservation` (BUD-17)."""
    if agent.reports_cost or not token_cap(state):
        return True
    return tokens_used(state) < token_cap(state)


RESERVE_FLOOR = 500_000      # tokens: the least an unpriced call is held to under a cap


def call_reserve(state):
    """The bound of one unpriced call's reservation under a cap: `[defaults]
    unpriced_call_reserve`, or, when that is 0, twice the largest measured unpriced call of the
    run so far and at least RESERVE_FLOOR. Twice: a reviewer's calls in one run were seen to vary
    by about that much; the floor covers a run whose first calls are small (BUD-17)."""
    fixed = int(state.get('unpriced_call_reserve') or 0)
    if fixed:
        return fixed
    return max(RESERVE_FLOOR, 2 * int(state['spend']['unpriced'].get('largest_call', 0)))


SERIAL_RESERVE = -1          # unpriced_call_reserve of a run recorded before BUD-17 (schema)


def bounded(state):
    """A run recorded before BUD-17 has `unpriced_call_reserve` SERIAL_RESERVE (set by
    `schema.migrate`): its unpriced calls keep reserving all that is left under the cap and start
    one at a time, as they did."""
    return state['unpriced_call_reserve'] != SERIAL_RESERVE


def token_remainder(state, agent, held=0):
    """What is left under the stop line for a call of an agent that reports no cost, after
    `held` (the reservations of the calls starting beside it). Recorded in the call's intent: it
    is what a call that ends without its final event and with unknown usage counts (BUD-20).
    0 for a priced agent or a run with no cap."""
    if agent.reports_cost or not token_cap(state):
        return 0
    return max(0, token_cap(state) - tokens_used(state) - held)


def token_reservation(state, agent, held=0):
    """The tokens a call of an agent that reports no cost is held to under the cap: what is left
    under the stop line after `held` (the reservations of the calls starting beside it in the same
    batch), bounded by `call_reserve`. It is recorded in the call's intent and counted only when
    the call's usage stays unknown (BUD-12). 0 for a priced agent or a run with no cap."""
    left = token_remainder(state, agent, held)
    return min(left, call_reserve(state)) if bounded(state) else left


PROBE_RESERVE_TOKENS = 20_000    # tokens: a qualification probe's bound; its prompt is a few hundred


def probe_reservation(state, agent, reserve=PROBE_RESERVE_TOKENS):
    """The tokens a qualification probe of an agent that reports no cost is held to under the
    cap: what is left under the stop line, bounded by `reserve` (`[defaults]
    probe_reserve_tokens`). Probe-sized, not a task call's bound, and bounded for every run, one
    recorded before BUD-17 included (BUD-24): a probe never holds everything left."""
    return min(token_remainder(state, agent), max(1, int(reserve)))


def admits_unpriced(state, held):
    """Whether one more unpriced call may join a batch whose unpriced calls already reserve
    `held` tokens: the first always may (`fits_tokens` decides it), another only while its whole
    bounded reservation fits under the line beside theirs. A run recorded before BUD-17 admits
    one. The cap stays a stop line: calls that each use more than they reserve can together
    pass it by that much each (BUD-17)."""
    if not held:
        return True
    if not bounded(state):
        return False
    return held + call_reserve(state) <= token_cap(state) - tokens_used(state)


def token_stop(state, what):
    """The stop at the cap. Its reason keeps measured and held tokens apart and names the calls
    held for unknown usage, so a cap reached by work reads differently from one reached by doubt."""
    counted = int(state['spend']['unpriced'].get('counted_calls', 0))
    if counted:
        used = (f"{tokens_measured(state)} measured + {tokens_held(state)} held for "
                f"{counted} call{'' if counted == 1 else 's'} with unknown usage = "
                f"{tokens_used(state)} of {token_cap(state)} tokens")
        calls = held_calls_phrase(state)
        used += f"; held: {calls}" if calls else ""
    else:
        used = f"{tokens_used(state)} of {token_cap(state)} tokens"
    return Exhausted(f"the token cap for agents that report no cost is used up ({used}) before {what}",
                     hint="--add-tokens N")


HELD_SHOWN = 3          # the held calls named in a stop reason and the Spend line, latest first
HELD_KEPT = 10          # the held calls kept in `spend.unpriced.held`; the counters keep the totals


def held_calls_phrase(state):
    """"tasks/…/round-1/invocation-1 (3.1 s, 5000 tokens; interrupted), …" of the calls held
    for unknown usage, latest first; '' for a run recorded before the calls were named."""
    unpriced = state['spend']['unpriced']
    held = unpriced.get('held') or []
    parts = []
    for h in reversed(held[-HELD_SHOWN:]):
        why = h.get('status', '') + (f": {h['error']}" if h.get('error') else '')
        parts.append(f"{h.get('invocation') or h.get('task')} ({h.get('seconds', 0):g} s, "
                     f"{h.get('tokens', 0)} tokens; {why})")
    more = int(unpriced.get('counted_calls', 0)) - len(parts)
    return ", ".join(parts) + (f" and {more} earlier" if parts and more > 0 else "")


def unsettled_usd(state):
    """Dollars held for calls of agents that report cost which ended without a price (a time-out,
    an error, an interruption). The call may have spent up to its cap, so the reservation is kept
    against the limit instead of being written off as $0. Runs recorded before this have none."""
    return float(state['spend'].get('unsettled', {}).get('usd', 0.0))


def counted_estimates_usd(state):
    """Estimated dollars of profiles that set `estimated_counts = true`: the owner chose to hold
    them against the dollar limit like known spend. Runs recorded before this have none."""
    return float(state['spend'].get('estimated_counted_usd', 0.0))


def fits(state, amount):
    spend = state['spend']
    held = spend['known_usd'] + unsettled_usd(state) + counted_estimates_usd(state)
    return (held < state['run_budget_usd']
            and round(held + spend['reserved_usd'] + amount, 6)
            <= state['run_budget_usd'])


def held_for_unknown(state, result, token_reservation, token_remainder):
    """The tokens a call with unknown usage counts against the stop line. Admission is bounded
    (BUD-17), settlement is not: a call that ended without its final event (a time-out, a kill, a
    provider failure after work began) may have used everything that was left when it started,
    so it counts that remainder, but never more than is left under the line now: calls started
    together share one remainder, so the first such call to settle brings the run to the line and
    the next holds only what its siblings left, never a second copy of the same allowance. A
    call that completed without reporting usage (a `command` agent) counts its reservation. An
    intent recorded before BUD-20 has no remainder: its reservation (BUD-20)."""
    counted = int(token_reservation)
    if token_remainder and not result.completed:
        left = max(0, token_cap(state) - tokens_used(state))
        counted = min(max(counted, int(token_remainder)), left)
    return counted


def settle(state, task_id, reservation, result, token_reservation=0, call=None, token_remainder=0):
    """`reservation` is positive only for an agent that reports cost (`cap_for`): a call of such
    an agent that ends without a cost keeps its reservation as unsettled spend, and its tokens are
    recorded with it, never on the unpriced line that the token stop line counts.
    Observed usage is never discarded: a quota refusal or an environment failure counts as
    nothing spent only with positive evidence that it was refused before any work and no
    contradictory usage. One that has usage (the provider's record says a session ran, then failed) is
    settled like any call that ended without a price.
    `token_reservation` (from the call's intent; 0 with no cap, and in an intent recorded before
    BUD-12, which `schema.migrate` gives that 0) is counted against the token stop line when an unpriced call's usage stays unknown: no
    terminal usage and no provider record. The run then stops conservatively instead of leaving
    the call out; `resume --add-tokens N` continues (BUD-12). A call that ended without its
    final event counts `token_remainder` instead, the rest of the line when it started
    (`held_for_unknown`, BUD-20). Such a call is named in
    `spend.unpriced.held` by `call` (its task and invocation directory) for the stop reason.
    A provider failure the adapter saw end before any work (`before_work`: a capacity refusal
    with no item event, no assistant message and no usage) spends nothing either (BUD-15); one
    after work began, with no usage, stays conservative."""
    spend, task = state['spend'], state['tasks'][task_id]
    spend['reserved_usd'] = round(max(0, spend['reserved_usd'] - reservation), 6)
    state['seconds'] += int(result.seconds)
    refused = (result.status in ('environment', 'quota', 'transient')
               and getattr(result, 'before_work', False) and not result.usage)
    if result.cost_usd is not None:
        spend['known_usd'] = round(spend['known_usd'] + result.cost_usd, 6)
        task['cost_usd'] = round(task['cost_usd'] + result.cost_usd, 6)
    elif refused:
        pass                                     # refused before any work: nothing was spent
    elif reservation > 0:
        unsettled = spend.setdefault('unsettled', {'usd': 0.0, 'calls': 0, 'tokens_in': 0,
                                                   'tokens_out': 0})
        unsettled['usd'] = round(unsettled['usd'] + reservation, 6)
        unsettled['calls'] += 1
        unsettled['tokens_in'] += int(result.usage.get('tokens_in', 0))
        unsettled['tokens_out'] += int(result.usage.get('tokens_out', 0))
    else:
        unpriced = spend['unpriced']
        unpriced['calls'] += 1
        unpriced['unknown_calls'] += int(not result.usage)
        if not result.usage and not refused and token_reservation > 0:
            token_reservation = held_for_unknown(state, result, token_reservation, token_remainder)
            unpriced['counted_calls'] = int(unpriced.get('counted_calls', 0)) + 1
            unpriced['counted_tokens'] = int(unpriced.get('counted_tokens', 0)) + int(token_reservation)
            unpriced['held'] = (unpriced.get('held') or [])[-(HELD_KEPT - 1):] + [{
                'task': task_id, 'invocation': (call or {}).get('invocation', ''),
                'seconds': round(float(result.seconds or 0), 1), 'status': result.status,
                'error': ' '.join(str(result.error or '').split())[:160],
                'tokens': int(token_reservation)}]
        measured = int(result.usage.get('tokens_in', 0)) + int(result.usage.get('tokens_out', 0))
        if measured > int(unpriced.get('largest_call', 0)):
            unpriced['largest_call'] = measured        # sizes the next bounded reservation (BUD-17)
        unpriced['tokens_in'] += int(result.usage.get('tokens_in', 0))
        unpriced['tokens_out'] += int(result.usage.get('tokens_out', 0))
        cached = min(int(result.usage.get('cached_tokens_in', 0) or 0),
                     int(result.usage.get('tokens_in', 0)))
        if cached or 'cached_tokens_in' in unpriced:
            # Part of tokens_in that the provider read from its cache (BUD-21), shown as a share.
            unpriced['cached_tokens_in'] = int(unpriced.get('cached_tokens_in', 0)) + cached
    estimated = getattr(result, 'estimated_usd', None)
    if estimated is not None:
        # An estimate from the profile's rates is never known spend (05, Honest accounting).
        spend['estimated_usd'] = round(spend.get('estimated_usd', 0.0) + estimated, 6)
        if result.estimated_counts:
            spend['estimated_counted_usd'] = round(counted_estimates_usd(state) + estimated, 6)
    record_windows(spend, getattr(result, 'limits', None))


OBSERVATIONS_KEPT = 50   # the latest window observations kept per window; `ranges` keeps the span


def record_windows(spend, limits, at=None):
    """Add one call's rate-limit window observation to the run's `spend.windows`, keyed
    "<kind>:<limit_id>" (and ":secondary" for a second window). The window belongs to the
    account, not the run: other use of the account moves it too, and calls that ran side by side
    each see the others' share. So the record keeps what was observed (BUD-21): the latest
    `observations` (`at`, `before`, `after`, `resets_at`) and, per reset of the window (its
    `resets_at`, the reset identity), the lowest and highest percentage seen (`ranges`), which
    does not depend on the order the calls ended in. `delta_percent`, the sum of the calls' own
    deltas, is kept for records read by older runners; it overstates parallel calls and is not
    shown. `at`: when the call ended (epoch seconds), now by default."""
    if not limits:
        return
    key = f"{limits.get('kind', 'codex')}:{limits.get('limit_id') or 'default'}"
    stamp = datetime.datetime.fromtimestamp(at if at is not None else time.time(),
                                            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for name, window in ((key, limits), (key + ":secondary", limits.get('secondary'))):
        if not window:
            continue
        w = spend.setdefault('windows', {}).setdefault(name, {
            'used_percent_first': window['used_percent_before'], 'delta_percent': 0.0})
        w.update(window_minutes=window.get('window_minutes'), plan_type=limits.get('plan_type'),
                 used_percent_last=window['used_percent_after'],
                 delta_percent=round(w['delta_percent'] + window['delta_percent'], 3))
        before, after = window['used_percent_before'], window['used_percent_after']
        reset = str(window.get('resets_at') or limits.get('resets_at') or '')
        w['observations'] = (w.get('observations') or [])[-(OBSERVATIONS_KEPT - 1):] + [
            {'at': stamp, 'before': before, 'after': after, 'resets_at': reset or None}]
        ranges = w.setdefault('ranges', {})
        # A window that reset during the call (its percentage fell) began the new range then.
        r = ranges.setdefault(reset, {'low': before if after >= before else after, 'high': after,
                                      'first_at': stamp})
        r.update(low=min(r['low'], before if after >= before else after), high=max(r['high'], after))


def window_ranges(w):
    """The observed ranges of one window, oldest reset first: [{low, high, first_at}]. A window recorded
    before the observations were kept has one, from its first and last readings."""
    ranges = w.get('ranges')
    if not ranges:
        return [{'low': w['used_percent_first'], 'high': w['used_percent_last']}]

    def order(item):
        # The reset time orders the windows whatever order the calls ended in.
        reset, r = item
        try:
            return (float(reset), r.get('first_at', ''))
        except ValueError:
            return (float('inf'), r.get('first_at', ''))
    return [r for _reset, r in sorted(ranges.items(), key=order)]

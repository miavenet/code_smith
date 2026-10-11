# Brief: split the pricing module

`src/pricing/engine.py` has grown to 1,400 lines: quoting, fee schedules and rounding live in one
module and share module-level state. Split it so each concern has its own module, with no change
to what callers see.

Callers import only from `pricing` (`pricing.quote`, `pricing.fee_for`, `pricing.round_price`,
`pricing.PricingError`); those names, their signatures and the errors they raise stay as they are.
The CLI `python3 -m pricing.cli` and its output format stay as they are.

The steps are given in the workflow, one task each. Do each step completely and nothing else.

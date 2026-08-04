"""
Which proposal deserves scarce capital, across both books, on one measured scale.

Phase 4's one new package. Five modules, built in dependency order:

    scoring    empirical E[R] per rupee-day, charged for friction   [Stage 4]
    proposal   the common shape both books emit                     [Stage 10]
    hurdle     opportunity cost, two regime buckets                 [Stage 10]
    policies   swing assignment | intraday stopping                 [Stage 10]
    allocator  select, basket recheck, buffered write               [Stage 10]

ONE DEPENDENCY RULE, AND IT IS STRUCTURAL.

    allocation -> analysis, intraday.cost_model, ai        allowed
    allocation -> execution                                FORBIDDEN

The allocator must never be able to reach an order path. That prohibition is
what makes shadow mode safe to run against a live book: a component that cannot
import the thing that places orders cannot place one by accident, however wrong
its arithmetic. It is verifiable by inspection, and tools/health checks it.
"""

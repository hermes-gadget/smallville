# Smallville economy and life simulation

## Goals and invariants

The life simulation is a deterministic, local state machine layered onto the
existing Reverie step loop. It creates no routine LLM or embedding traffic,
does not alter the sequential perceive/six-worker decide contract, and adds
only optional fields to movement and persona-state responses. A normal tick is
bounded by the 25 residents and four shops and is expected to complete in less
than 50 ms.

Runtime data remains under the ignored
`environment/frontend_server/storage/public_sim/` and
`environment/frontend_server/temp_storage/` trees. All JSON replacements are
atomic. The implementation and offline tests must never create, change, or
delete files in those live trees, and must never manage the user services.

## Step integration

For each consumed `environment/<step>.json`, Reverie keeps its existing order:

1. synchronize backend persona tiles;
2. drain queued admin commands and run one economy tick;
3. collect perception sequentially without LLM calls;
4. retrieve, plan, reflect, and execute in the existing six-worker pool;
5. write the additive movement payload and persona scratch state;
6. advance the step and wall-clock-paced game time.

The economy call is isolated by a broad exception boundary in `reverie.py`.
Failure returns the last scratch snapshot and can never abort the town step.
The tick observes the action selected on the preceding step at the location
just reached, which makes work, shopping, sleep, chat, and study accounting
match physical presence.

Movement history is pruned once at loop startup and incrementally thereafter,
retaining the newest 500 numeric movement files. The deployed headless run
length becomes 1,000,000,000 steps so systemd is not cycling the process every
100,000 steps.

## Persistent schema

`storage/<sim_code>/economy/economy_state.json` is the authoritative versioned
state. It contains:

- `residents`: balance, lifetime earnings/spending, debt, wealth tier,
  housing/health/admin status, hunger/energy/social needs, education score,
  rent and bankruptcy counters, medicine/rest state, and action idempotency;
- `shops`: sector, owner/staff, cash, and physical goods with price, stock,
  and capacity;
- `bank`: banker, cash (initially $20,000), interest/fee totals;
- `treasury`: municipal cash;
- clock cursors for elapsed-time, daily, and monthly processing; and
- a bounded set of completed admin command IDs for replay protection.

Money is rounded to cents after every mutation. Initial resident balances are
a stable SHA-256-derived value from $80 through $200, so fresh simulations are
varied but reproducible. Invalid or future-dated clock deltas are treated as
zero. Backward-compatible normalization fills missing fields whenever the
schema grows.

`economy/economy_feed.json` is a separate atomically replaced list capped at
300 entries shaped as `{ts, step, text}`. Every purchase, pay event, rent,
interest charge, illness/recovery, education milestone, bankruptcy, and status
transition is appended; public endpoints return no more than the newest 200.

Persona scratch mirrors `economy_balance`, `economy_total_earned`,
`economy_total_spent`, `economy_debt`, `economy_status`, `health_hunger`,
`health_energy`, `health_social`, and `education_score`. Missing keys load with
safe defaults, preserving old personas. Movement adds
`economy: {balance, status}` per persona.

## Jobs, shops, and transfers

Elapsed game minutes are derived from consecutive game-clock timestamps.
Workers earn only while physically present in their configured sector/arena.
Ordinary wages are $12-$16/hour; shop staff are paid from shop cash, the mayor
earns $25/hour from the treasury while at the college library, the banker is
paid from bank cash, and externally employed residents receive ordinary wages.
Tom can therefore work separately as Willows owner or as mayor. Students earn
no hourly wage and receive a $6 daily treasury stipend. Jane receives $40 per
day from Tom, and Maria's cafe income is tips rather than a wage.

The physical shops are Hobbs Cafe, The Rose and Crown Pub, The Willows Market
and Pharmacy, and Harvey Oak Supply Store. Each owns capped stock and cash.
Stock returns to capacity at day start. A resident can buy once per distinct
action/location key: explicit menu words select matching goods, while generic
eating or shopping selects an appropriate in-stock menu item. The cheapest
affordable match wins; a reproducible minority of rich purchases selects the
most expensive match. Payment moves from the resident to shop cash and reduces
stock. Food restores hunger and pharmacy medicine participates in illness
recovery.

Wealth tiers are `broke < $5`, `tight < $25`, `stable < $150`,
`comfortable < $500`, `wealthy < $2,000`, and `rich >= $2,000`. Tier changes
and entry to the rich list are feed events. A zero-balance purchase attempt
causes Yuriko to declare bankruptcy, adds a bank fee to debt, and the third
declaration makes the resident homeless.

Daily rent ranges from $5 to $15 by housing type and is paid to the treasury.
Shortfalls become debt; repeated unpaid rent creates a deterministic
homelessness risk. Homeless residents stop owing rent. Debt incurs 0.25% daily
interest: available cash pays the bank, otherwise the unpaid interest compounds
into debt.

## Health and education

Hunger, energy, and social need start at 75 and decay per elapsed game hour.
Food restores hunger, sleep restores energy, and chat/cafe/pub time restores
social need. A broke resident below 20 hunger becomes `hungry` and generates a
feed transition.

Illness is selected deterministically at approximately 1% per resident-day.
Recovery requires pharmacy medicine plus accumulated sleep/rest. Students gain
education score only at Oak Hill College. Crossing a calendar-month boundary
emits one progress-flavor entry per student.

The public status uses this precedence: explicit admin status, homeless, ill,
hungry, then wealth tier.

## Admin command protocol

Django `POST /admin/command/` reuses `_is_admin_request` and appends a UUID
command to `temp_storage/admin_commands.json`. A separate lock file coordinates
the Django writer and Reverie reader; the queue itself is always replaced
atomically. Commands are shaped as `{id, cmd, args, status}` and completed in
place with `result` (and an error for failures).

Before each tick Reverie reclaims pending/processing commands and handles:
`give_money`, `bankrupt`, `make_rich`, `set_status`, `inject_event`,
`broadcast`, `restock`, `adjust_price`, `add_good`, `transfer`, and
`set_pacing`. State records completed IDs, so a crash between state and queue
updates cannot apply monetary mutations twice.

Event injection calls `a_mem.add_event` at poignancy 5 with a real embedding.
A broadcast embeds its description once and shares the vector across resident
memories. At most two distinct embedding calls are allowed during one drain;
the deterministic economy path itself makes none.

## Django read model and failure behavior

`GET /get_economy/?sim_code=public_sim` returns public resident
balances/statuses, shops, treasury, bank, and a feed tail. `GET
/get_economy_feed/` returns `{entries: [...]}`. Simulation codes and persona
names receive the existing traversal checks. Missing, partial, or concurrently
replaced files produce an empty/default response rather than a server error.

Verification uses only temporary directories and fake personas: compile all
touched Python, run Django's system check, execute a deterministic smoke tick
covering pay/purchase/health/admin behavior, and assert repeated normal ticks
stay below the 50 ms budget. No real simulator or server process is started.

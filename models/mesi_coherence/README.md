# MESI Cache Coherence — PlusCal Model

A textbook snoopy-bus MESI (Modified, Exclusive, Shared, Invalid) cache
coherence protocol modelled in PlusCal with C-syntax.

## Architecture

```
  ┌───────┐          ┌───────┐
  │ Proc1 │          │ Proc2 │
  │ cache │          │ cache │
  └───┬───┘          └───┬───┘
      │   Bus Request    │
      ├──────────────────┤
      │      ┌─────┐     │
      │      │ Bus │     │
      │      └──┬──┘     │
      │         │        │
      │    ┌────┴────┐   │
      │    │ Memory  │   │
      │    └─────────┘   │
      └──────────────────┘
```

## Parameters

| Constant      | Values                           | Description                        |
|---------------|----------------------------------|------------------------------------|
| `Operation`   | `"Read"`, `"Write"`              | What Proc1 does                    |
| `InitState`   | `"M"`, `"E"`, `"S"`, `"I"`      | Proc1's initial cache line state   |
| `RemoteState` | `"M"`, `"E"`, `"S"`, `"I"`      | Proc2's initial cache line state   |

### Parameter sweep (32 combos)

Not all combinations are physically valid (e.g., both caches in Modified is
impossible).  The model still accepts them — the coherence invariant catches
violations.

## MESI State Transitions

| Initial | Bus transaction | Final state |
|---------|----------------|-------------|
| I       | BusRd (miss)   | E or S      |
| I       | BusRdX (miss)  | M           |
| E       | Local write    | M (silent)  |
| E       | Snoop BusRd    | S           |
| E       | Snoop BusRdX   | I           |
| S       | BusUpgr        | M           |
| S       | Snoop BusRd    | S           |
| S       | Snoop BusRdX   | I           |
| M       | Local write    | M (silent)  |
| M       | Snoop BusRd    | S (flush)   |
| M       | Snoop BusRdX   | I (flush)   |

## Running

```bash
# Translate PlusCal → TLA+
java -cp tla2tools.jar pcal.trans mesi_coherence.pcal

# Check one configuration
java -cp tla2tools.jar tlc2.TLC mesi_coherence -config mesi_coherence.cfg

# Or with the PlusCal Explorer extension: Ctrl+Shift+R
```

## Properties checked

- **MESICoherence** (invariant):
  - No two caches both Modified
  - No Modified + Exclusive simultaneously  
  - If both caches have data, values agree
- **RequestCompletes** (liveness): every request eventually finishes

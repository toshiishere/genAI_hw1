# Request Flow Pattern

Use this pattern when creating another skill under `skills/request-flows/`.

## Minimal Shape

```md
---
name: your-flow-name
description: When to use the flow, what it resolves, and whether it bypasses the general chat model.
---

# Your Flow Name

## Trigger
- Example phrasing

## Flow
1. Detect the request type.
2. Resolve required inputs.
3. Call the tool or deterministic path.
4. Return the result directly.

## Output Shape
- The fields or sections the reply should include

## Interpretation Rules
- Add explicit decision rules when the user may ask for advice, not just data
- Example: translate raw tool output into yes/no, recommend/avoid, or likely/unlikely
- State which time window or condition the advice must be based on

## Guardrails
- Failure handling
- Priority rules
- No fabrication rules
```

## Good Sibling Examples

- `skills/request-flows/calendar-request/`
- `skills/request-flows/reminder-request/`
- `skills/request-flows/maps-request/`
- `skills/request-flows/stock-request/`

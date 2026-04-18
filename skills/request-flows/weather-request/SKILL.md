---
name: weather-request
description: Use when a user asks for weather, forecast, temperature, rain, wind, or similar conditions for a place, including decision questions like whether they need an umbrella tomorrow. Resolve a user-specified location first; if none is given, fall back to IP-based geographic lookup. Combine tool output with the user’s question and return a direct interpreted answer instead of routing through a general chat model. This skill is also a template flow for adding other request-specific skills under skills/request-flows/.
---

# Weather Request Flow

This skill defines a request-routing flow for weather questions.

## Trigger

Use this skill when the user asks things like:

- "What's the weather in Taipei?"
- "Will it rain tomorrow?"
- "How hot is it outside?"
- "Do I need an umbrella?"
- "Do I need an umbrella tomorrow?"
- "Should I bring a jacket?"

## Flow

1. Detect whether the request is about weather or forecast conditions.
2. Detect the time scope:
   current, today, tomorrow, or another explicit forecast window.
3. Detect whether the user wants:
   raw conditions, rain likelihood, umbrella advice, clothing advice, or another weather-based recommendation.
4. Extract a location from the user message if one is present.
5. If a location is present:
   use an MCP weather-capable tool or service with that location.
6. If no location is present:
   resolve location from IP/geographic context first, then use the weather tool.
7. Request the correct weather data for the question:
   current weather for current-condition questions, forecast data for tomorrow/future questions.
8. Combine the weather data with the user’s intent and answer the actual question.
9. For recommendation questions such as umbrellas:
   do not stop at reporting forecast numbers; translate them into a direct yes/no recommendation with a short reason.

## Output Shape

Return a short answer with:

- resolved location
- requested time scope
- direct answer to the user’s question first
- condition summary
- temperature or forecast note
- optional feels-like, humidity, wind, or precipitation note
- whether location came from user text or fallback lookup

For umbrella-style questions, prefer this shape:

- `Yes` or `No` in the first sentence
- one short reason tied to precipitation or rain likelihood
- one supporting weather detail if useful

## Interpretation Rules

- If the user asks `Do I need an umbrella?`:
  answer `Yes` when rain, showers, storms, or meaningful precipitation risk is indicated for the requested time window.
- If the precipitation signal is low or absent:
  answer `No`.
- If the data is mixed or uncertain:
  answer `Probably yes` or `Probably no`, then explain briefly.
- If the question is specifically about `tomorrow`:
  use forecast data for tomorrow rather than current conditions.
- If the user asks for raw weather only:
  do not force a recommendation.

## Guardrails

- Prefer the user-specified location over inferred location.
- If location resolution fails, say so clearly and ask for a city or region.
- If the tool returns partial data, answer with what is available instead of fabricating.
- Answer the user’s actual decision question, not just the underlying forecast query.
- Never use current weather to answer a tomorrow question if forecast data is available.
- Keep the response concise unless the user asks for more detail.

## Extension Pattern

To add another flow under `skills/request-flows/`:

1. Copy this folder to a new sibling directory.
2. Change the frontmatter `name` and `description`.
3. Replace the trigger phrases and flow steps for the new request type.
4. Keep the same structure: Trigger, Flow, Output Shape, Guardrails.

For a compact pattern you can reuse, see [references/flow-pattern.md](references/flow-pattern.md).

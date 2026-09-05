# The prompt this project answers

This repo is our entry for the **Fall 2026 MISO Xtern Challenge**, a
team hackathon run by [TechPoint](https://techpoint.org) and sponsored by
MISO. Teams choose one of several prompts; we chose Prompt 1.

## Prompt 1 — Intelligent Navigation of MISO's Public Information

> How can MISO use AI to improve accessibility, discoverability, and
> usability of public data? Build an AI tool that helps users find public
> data on MISO's website. The goal is to reduce routine information requests
> to MISO's CSR (Customer Support and Response) and External Affairs teams.
>
> **Final deliverable:** an AI tool built on top of the website, plus a
> presentation explaining the process and design.

The prompt is intentionally open-ended — MISO's mentors said so directly.
Deciding *what* to build is part of the challenge.

## Who is MISO, and why is this a real problem

MISO (Midcontinent Independent System Operator) operates the electricity
grid and wholesale energy markets across 15 U.S. states and Manitoba,
serving about 45 million people. It doesn't generate power or own
transmission lines — it coordinates the system, like air traffic control
for electricity.

MISO publishes an enormous amount of public data: live grid feeds (fuel
mix, load, wind and solar output, prices), a Market Reports section with
eleven categories of downloadable files, planning documents, regulatory
filings, and a help center. The problem is findability. The site search is
weak, the reports section has almost no filtering, and the audience ranges
from curious citizens and journalists to utility analysts and regulators —
"very little to a lot" of both energy and technical knowledge, in MISO's
own words.

So people give up and email MISO's human teams instead. External Affairs
owns the stakeholder relationships; CSR fields the actual requests. Routine
"where do I find X?" questions consume time those teams could spend on
questions that genuinely need a person.

## What "success" means

Every routine question the tool answers well is an email that never gets
sent — that's the deflection the prompt asks for. The kinds of requests
those teams actually receive, roughly:

1. **"What's the grid doing right now?"** — current fuel mix, load,
   renewables share (public, journalists, students)
2. **"Where's the historical data?"** — prices, load, settlements
   (analysts, researchers, members)
3. **"How does this process work?"** — interconnection queue, capacity
   auctions, planning (utilities, market participants)
4. **"What's MISO's position or filing?"** — regulatory filings,
   reliability assessments (regulators, media)

Our answer to the prompt — a chat assistant over MISO's own public data,
with sources and freshness on every answer — is described in the
[main README](../README.md).

## Ground rules from MISO's mentors

- **No scraping miso.org** — the site has anti-scraping protection, and
  abuse gets an IP banned. Public APIs and politely-fetched documents only.
- **Respect the API rate limit** — at most one request per endpoint per
  minute against `public-api.misoenergy.org`.
- A working live demo counts for a lot; the thought process and design
  reasoning count for more than polish.

<!-- OPENSPEC:START -->

# OpenSpec Instructions

These instructions are for AI assistants working in this project.

Always open `@/openspec/AGENTS.md` when the request:

- Mentions planning or proposals (words like proposal, spec, change, plan)
- Introduces new capabilities, breaking changes, architecture shifts, or big performance/security work
- Sounds ambiguous and you need the authoritative spec before coding

Use `@/openspec/AGENTS.md` to learn:

- How to create and apply change proposals
- Spec format and conventions
- Project structure and guidelines

Keep this managed block so 'openspec update' can refresh the instructions.

<!-- OPENSPEC:END -->

# YNAB Butler Development Guidelines

Auto-generated from all feature plans. Last updated: 2025-11-01

## Active Technologies

- Python 3.10+ (align with repo baseline) + `imapclient` for IMAP access, `email` stdlib parsing, `pyzipper` for
  AES-encrypted ZIP handling, existing `pandas`, `requests`, `ynab` SDK (001-email-import)

## Project Structure

```text
src/
tests/
```

## Commands

cd src [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES]
pytest [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] ruff check .

## Code Style

Python 3.10+ (align with repo baseline): Follow standard conventions

## Recent Changes

- 001-email-import: Added Python 3.10+ (align with repo baseline) + `imapclient` for IMAP access, `email` stdlib
  parsing, `pyzipper` for AES-encrypted ZIP handling, existing `pandas`, `requests`, `ynab` SDK

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->

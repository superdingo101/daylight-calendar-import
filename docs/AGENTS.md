# Daylight Calendar Import documentation

This folder is the authored Mintlify site for **Daylight Calendar Import**, published to a separate free Mintlify site. Calendar Card is documented in its own repository.

- Use short sentence-case headings, active voice, second person, and plain language.
- Follow the Calendar Card documentation's Mintlify design conventions: MDX frontmatter, `<Steps>`, `<Note>`, `<Warning>`, and concise navigation groups.
- Document the **released self-hosted Home Assistant integration**, not the internal hosted backend. Do not describe unreleased or experimental behavior as available.
- Never imply that imports automatically write calendar events. Approval is explicit. Describe uncertain calendar writes conservatively.
- Treat Direct IMAP as sensitive: recommend a dedicated folder/mailbox; warn about the unread backlog and AI provider privacy.
- Keep provider-specific capabilities qualified. AI Task attachment support does not prove a model accepts scanned PDFs.
- Source for details: the feature's tests and the existing `docs/*.md` reference guides. Preserve those guides; public MDX pages should link to technical references where relevant.
- The publishing branch `docs-live` is managed from stable release tags. Write/edit docs on ordinary development branches, not directly on `docs-live`.
- Preview from `docs/` with `mint dev`; validate with `mint validate` and `mint broken-links`.
- Examples use synthetic events, calendars, and sender addresses. Never include actual source content, API keys, or credentials.

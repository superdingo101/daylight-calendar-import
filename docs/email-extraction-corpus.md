# Email and newsletter extraction cases

Four synthetic cases in `tests/fixtures/extraction/email-*.json` extend the
[extraction corpus](extraction-corpus.md) with realistic email structures. Sender
addresses, links, names, and venues are invented; reserved `.example` domains
avoid using real mailboxes. Every case fixes the received/reference date to
October 1, 2026, and declares the Home Assistant timezone and source kind.

| Case | Expected drafts | Details under test |
| --- | ---: | --- |
| `email-sports-24-games` | 24 | A complete U12 schedule with repeated opponents, distinct game numbers, alternating start times, three venues, and shared equipment instructions. |
| `email-birthday-invitation` | 1 | An invitation with its address, supplies, and RSVP contact; an undated RSVP request does not become an extra event. |
| `email-registration-and-camp` | 2 | A dated all-day registration reminder with a form URL, separate from the later timed camp with equipment instructions. |
| `email-school-newsletter` | 6 | Multiple newsletter sections, timed activities, a single-day closure, an inclusive three-day break, links and instructions, plus undated announcements that produce no extra drafts. |

The existing parameterized replay test automatically includes these cases and
passes each recorded provider response through the real AI Task/parser boundary.
`tests/test_email_corpus_contract.py` independently checks sports rows against
normalized drafts, deadline/event separation, invitation details, newsletter
completeness, and exclusive all-day end dates. These are fixture and integration
boundary checks, not evidence of live AI extraction quality. The optional corpus
evaluator can submit the same source text to an actual provider outside required
CI; no live provider, network request, or credentials are needed for these tests.

Existing routing fixtures separately cover forwarded/quoted messages, multiple
calendar aliases, conferencing details, deduplication, and adversarial content.

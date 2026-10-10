# Credits

This project is a personal, non-commercial derivative of **Mark LI**, an
open-source voice assistant by **FatihMakes**.

- Upstream project: https://github.com/FatihMakes/Mark-LI
- Upstream author: FatihMakes — https://www.youtube.com/@FatihMakes
- Upstream licence: Creative Commons Attribution-NonCommercial 4.0
  (CC BY-NC 4.0), reproduced unmodified in [`LICENSE`](LICENSE)

The original copyright notice is retained in full. This fork does not claim
authorship of the upstream engine, and inherits the non-commercial restriction:
it may not be used commercially.

## What this fork changes

| Area | Change |
| --- | --- |
| Identity | Renamed to AURA; personalised for a single user |
| Credentials | `core/keyring.py` — key **pools** with automatic rotation, replacing the single-key config |
| Model routing | `core/ai_router.py` + `core/cloud_llm.py` — Gemini falls back to OpenRouter → NVIDIA → Sarvam instead of failing on a quota wall |
| Voice quota | `gemini_voice_api_key` reserved for the Live session so text work can never mute the assistant |
| Localisation | Turkish-language defaults replaced with English / Hindi |

API providers used: Google Gemini (incl. the Live API), OpenRouter, NVIDIA NIM
and Sarvam AI. Each remains subject to its own terms of service.

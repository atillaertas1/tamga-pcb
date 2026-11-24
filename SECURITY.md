# Security policy

## Supported versions

tamga-pcb is young; security fixes go into the latest release only.

| Version | Supported |
|---|---|
| 0.1.x | yes |

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately through GitHub:
[Security → Report a vulnerability](https://github.com/atillaertas1/tamga-pcb/security/advisories/new).

Include what an attacker can do, the steps or a proof of concept, and the version and OS you tested on. You'll get
an answer within a week. Once a fix is released, the advisory is published and you're credited unless you'd rather
not be.

## Scope

tamga-pcb runs on your machine with your user's permissions. It reads and writes KiCad project files, starts
`kicad-cli`, KiCad's Python and Java (Freerouting) as subprocesses, downloads Freerouting from its GitHub releases,
talks to the JLCPCB/LCSC and EasyEDA APIs, and `tamga watch` serves a page on `127.0.0.1`.

Things we want to hear about, for example:

- a tool call or a crafted circuit spec, board or part file that writes outside the project or runs unintended commands
- the live view being reachable from other machines, or serving files outside the project
- downloaded or imported data (Freerouting, LCSC/EasyEDA parts) being used in a way that lets it run code

Out of scope: bugs in KiCad, Freerouting or easyeda2kicad themselves (report those upstream), and what an MCP client
or model chooses to do with the tools it was given permission to call.

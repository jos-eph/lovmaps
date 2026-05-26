# Security Policy

## Reporting a vulnerability

If you believe you have found a security issue in this repository or in any
artefact it publishes (the `.pmtiles` / `.pbf` files distributed via GitHub
Releases), please report it privately by email to **josephdm@gmail.com**.

Please include:

- A description of the issue and its impact.
- The steps required to reproduce it.
- The relevant commit SHA, release tag, or asset filename.

You should expect an acknowledgement within a reasonable time, given that
this is a single-maintainer nonprofit project. GitHub's **private
vulnerability reporting** is also enabled if you prefer to file through the
GitHub UI.

## Bug bounty

No bug bounty is offered. This is a nonprofit project. Thanks for helping
anyway.

## Supported versions

Only the **latest release** is supported. Older dated releases
(`tiles-<DATE>`) remain available for reproducibility but will not receive
security fixes; fixes land in the next dated release and in `current`.

## Scope

In scope: the code in this repository and the pipeline that produces the
released `.pmtiles` and `.pbf` assets.

Out of scope: vulnerabilities in upstream data (OpenStreetMap, Geofabrik,
Protomaps) or in third-party tools (`osmium-tool`, `tippecanoe`) — please
report those to their respective projects.

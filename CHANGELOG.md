# Changelog

All notable changes to ovc-backend. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Each version is a git tag
(`0.1.1`, no `v` prefix) with a matching GitHub Release and container image
(`ghcr.io/claudio-azevedo/ovc-backend:<version>`). See README "Releasing".

## [Unreleased]

## [0.2.0] - 2026-10-03

### Added

- Audit log: every change made through the API (VM create / clone / delete /
  power and management actions / folder move / tags, folders, clusters, host
  membership and actions, VLANs, tags, agent binaries, forced lock releases) is
  recorded with who, when, the target's name and a before/after diff.
  Agent-task events start `pending` and the worker settles them to the task's
  outcome. VMs that vanish from a host's inventory (deleted outside OVC) are
  recorded as system events. `GET /audit-events` (admin only, filters, cursor
  pagination). Kept `OVC_AUDIT_RETENTION_DAYS` (default 365, `0` = forever).
  Migration 0005. Run the API and the worker from the same release - the
  worker is what settles event outcomes.

## [0.1.1] - 2026-10-03

### Added

- VM tags: a global catalog of categories and tags with colours from a fixed
  10-name palette (`/tag-categories`, `/tags`, admin-only writes), and
  `PUT /vms/{id}/tags` for anyone who can see the VM. A VM holds at most one
  tag per category. Migrations 0002 and 0003.
- Host actions (`POST /hosts/{id}/actions/{action}`): Failover Cluster node
  pause / resume (with drain and fallback), host restart, forced hardware
  and inventory refresh. Disruptive actions are admin-only.
- VM guest OS and console thumbnails from the agent's `vm_inventory`:
  `guestOs` on VMs and `GET /vms/{id}/thumbnail` (JPEG, `X-Captured-At`).
  Migration 0004.
- Richer host hardware: NIC driver / firmware / link details, vSwitch and
  SET teaming info, Fibre Channel HBAs.
- The app version (OpenAPI `info.version`) comes from `pyproject.toml`.

### Changed

- VM placement rules on create, clone and storage move: clustered hosts
  accept only Cluster Shared Volumes; standalone hosts reject `C:` unless it
  holds the Hyper-V default VM path.
- Migrating a VM between cluster nodes requires High Availability.
- Python 3.14 and updated dependencies.

### Fixed

- Agent binary upload accepts only `.exe` files.

## [0.1.0] - 2026-09-20

First public release.

[Unreleased]: https://github.com/claudio-azevedo/Open-vCenter-Backend/compare/0.2.0...HEAD
[0.2.0]: https://github.com/claudio-azevedo/Open-vCenter-Backend/compare/0.1.1...0.2.0
[0.1.1]: https://github.com/claudio-azevedo/Open-vCenter-Backend/compare/0.1.0...0.1.1
[0.1.0]: https://github.com/claudio-azevedo/Open-vCenter-Backend/releases/tag/0.1.0

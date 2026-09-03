# Notes: backend-aware install/doctor terminal detection

- Built: `environment.detect_usable_backends()` + `run_doctor_checks()`
  (Increment 1 only). `check_environment()`/`ensure_environment()` untouched.
- Increment 2 (cmux) knowingly deferred — spike in
  `docs/research/2026-09-03-cmux-backend-spike.md` is unrun (needs a physical
  macOS 14+ cmux install). Revisit AD4/Implementation Order steps 7-10 once
  someone runs it.
- Review: `.claudespace/reports/install-terminal-detection-review.md` (PASS,
  no findings).

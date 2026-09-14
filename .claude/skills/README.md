# Vendored skills

`ponytail*` come from [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)
(MIT), v4.9.0, commit `356918e`. They are code-review and coding-style skills, not
part of the war room itself: `/ponytail-review` hunts over-engineering,
`/ponytail` keeps new code minimal.

Re-vendor with:

    git clone --depth 1 https://github.com/DietrichGebert/ponytail /tmp/ponytail
    cp -r /tmp/ponytail/skills/* .claude/skills/

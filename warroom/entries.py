"""The 24 Horas de Portugal 2026 entry list, as published by the organisers.

Apex does not have to spell a team the way the entry list does — ours is
"TPC CIAO CUORE" here and may be "TPC", "TPC CIAO CUORE" or something else
again on the timing screen — so this is reference data, never a key.  It is
used for two things:

* the category of a team when the feed's own class column is blank, and
* checking on Saturday morning that every entry actually turned up in the
  feed, which is when a name that does not match is cheap to fix.

Read off the organisers' entry graphic.  The list is one short: the last
entry was obscured, so ``COMPLETE`` is False and nothing here should be
treated as "these are all the teams".
"""

# (team, category).  Country is deliberately not recorded: the flags were
# readable enough to guess at and not readable enough to be right.
ENTRIES = [
    ("MATRAX", "PRO"),
    ("EL PUEBLO ENDURANCE", "PRO"),
    ("NACIONAL KART NEW GENERATION", "AM"),
    ("MICROÁGUA", "PRO"),
    ("TPC CIAO CUORE", "AM"),

    ("STCAR", "PRO"),
    ("GAUSELMANN RACING", "PRO"),
    ("CENTURY 21 SUNOCO", "AM"),
    ("OK LISBOA", "AM"),
    ("STF BY KARTCUP", "PRO"),

    ("STF PERFORMANCE", "PRO"),
    ("STF ENDURANCE", "PRO"),
    ("MASTER FUSION", "PRO"),
    ("P26R SPORT", "AM"),
    ("INWITO RACING TEAM", "PRO"),

    ("JUNIOR MOTORSPORT", "AM"),
    ("TRANS MLS RACING TEAM", "AM"),
    ("KOTM", "PRO"),
    ("WARMUP PRO", "PRO"),
    ("BAVOVNA RACING", "AM"),

    ("RODEX", "PRO"),
    ("TRACK LIMITS BY SAINTES", "PRO"),
    ("TRACK LIMITS SPORT", "PRO"),
    ("TFD I", "PRO"),
    ("TFD II", "PRO"),

    ("VLTK RACING", "AM"),
    ("JURASSIC KART", "AM"),
    ("JURASSIC KART RAPTOR", "AM"),
    ("KARTINGNOW ENDURANCE", "PRO"),
    ("HELP YOUR CAR", "AM"),

    ("FAST ROOKIES", "PRO"),
    ("BEL-AMIS PRO", "PRO"),
    ("MEMO RACING TEAM", "PRO"),
    ("URT ASTRO", "PRO"),
    ("CREATIA LTDL RACING", "PRO"),

    ("KMRS RACING", "PRO"),
    ("SPACE COWBOYS RACING", "PRO"),
    ("TR SPORTS", "AM"),
    ("ASCENSONDA", "AM"),
]

# One entry on the organisers' graphic was obscured and is not in the list.
COMPLETE = False

OURS = "TPC CIAO CUORE"

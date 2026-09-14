# Counting karts automatically

The timing feed identifies **teams** — a race number and a name that stay put
all race. It says nothing about which **kart** a team is sitting in, because
the organisers hand karts out in the pit lane and no transponder moves with
them. In a race where every stop means a new kart, keeping that book by hand is
a full-time job for one person, and it is wrong within the hour.

This is how the war room keeps it instead.

## The model

Karts wait in pit **lanes**. A team that stops hands its kart to the back of a
lane queue and takes the kart at the front of that same queue — first in, first
out, the way the marshals work.

* **Stops are detected from the feed.** The pit counter ticks, or a lap comes in
  a whole pit-lane slower than that team's own pace. Nobody presses a button
  when a rival stops.
* **The one human input is which lane.** That is the single thing the feed
  cannot see. Tap it and everything else follows: which kart went out, who
  holds what now, and every lap that kart has run since.
* **With one lane there is nothing to tap** — the count is automatic, except
  when two karts are in the pit lane at the same moment, when the war room asks
  who went out first rather than guessing.

Two answers are available on any stop: the lane, and `Type the kart` when
someone reads the number off the nose cone. Any mis-tap is one `undo` away.
Where an event does *not* swap karts at every stop, turn off "Karts are swapped
at every stop" in Settings and a third answer appears for driver-only stops.

### Before the start

Type each team's starting kart once, by clicking its kart cell in the timing
table, and drop the spare karts into the lanes on the pit phone. From then on
the book keeps itself.

## Rating the karts

The point of counting them is knowing which ones are quick — and a kart is only
as fast as the driver in it, so raw lap times rank drivers, not karts. The war
room fits an additive model to every lap the field runs:

    lap  ≈  baseline(t)  +  pilot_effect  +  kart_effect

`baseline(t)` is the rolling median of the whole field, which absorbs anything
that hits everyone equally: track evolution, temperature, rubber, night
running, safety cars. The other two are fitted *jointly*, by ridge-penalised
alternating least squares. Because karts pass through many teams over a long
race, the two separate: the model learns that a team is half a second slow and
stops blaming its kart for it. A pro team's kart is not flattered by the pro
team, and an amateur team's rocket is still reported as a rocket — which is the
whole reason for doing it this way rather than averaging lap times per kart.

Scores read as a delta in seconds against the best kart in the fleet, so the
quickest kart shows `0.00`:

| Label | Delta | |
|---|---|---|
| Godlike | < 0.15 | take it |
| Very Good | < 0.40 | |
| Good | < 0.65 | |
| OK | < 1.00 | |
| Bad | ≥ 1.00 | a second a lap, every lap |
| Unknown | — | not enough to say yet |

**PRO and AM** are read from the feed's category column and filter the timing
table; a stop on the phone shows the category next to the team. The rating does
not need them — each team's own pace is already subtracted, which is a finer
correction than a class average.

**Unknown is a real answer, not a gap in the data.** A kart needs clean laps
from more than one team before its pace can be told apart from the driver's.
Before the first swaps, every kart is Unknown, and it should be: at that point
nothing in the timing screen can separate a good kart from a good driver.
Thresholds are tunable per track under `karts.rating` in `config.json`.

## The phone in the pit lane

`/pit` is where the race is actually run, so it is built for one hand and a
glance:

* **Our own stop is at the top**, with the minimum pit time counting down in
  the largest type on the page. Nobody presses BOX.
* **A new question buzzes the phone** and keeps the screen awake, because a
  question nobody notices is a kart nobody counted.
* Lane buttons are thumb-sized and say which kart is next out of each lane.
* Everything is two taps from the top of the page: the lane, or `undo`.

The war room screen shows the same questions for whoever is on the pit wall,
but the phone is the one that has to work.

## What the feed drives on its own

* The **race clock** comes from the timing tower's header when it is there, and
  falls back to our own clock when the feed goes quiet. The topbar says which.
* **Our own pit stop** starts and ends itself, with no button. The box clock
  starts the moment the feed shows our kart in the lane — earlier than the pit
  counter, which only ticks once the stop is registered — and the next stint
  starts when the kart is back out. Turn it off in Settings if you would rather
  press the button.
* **Pit stop counts** come from the timekeepers' column, since that is the
  number that settles a protest.

## Rehearsing without a race

    python3 mock_race.py --speed 60 --seed-karts

A full mock field, kart swaps, pit lanes and a race clock, at any speed. Open
the war room on `:8080`, the pit phone on `/pit`, and `/mock/truth` to see how
many karts the war room has right — that is the number to watch while
practising. `--lanes 1` shows the fully automatic case.

## Accuracy

From the simulated race in `test_mock_race.py`, six hours and ~250 stops:

* **Two lanes, answered promptly: every kart correct**, all race.
* **One lane: at most one kart out of place.** When two teams are in the pit
  lane together, whoever reaches the front kart first decides who gets it; the
  war room asks whenever it can see the overlap, but a stop that looked
  unambiguous can still turn out to have been simultaneous.
* Kart ratings track the simulator's hidden kart qualities with a rank
  correlation above 0.75, with the fast teams' karts deliberately mis-allocated
  to try to fool them.

## Teaching it a new event

If the war room reads the wrong columns at a track, record the real feed from a
machine that can reach Apex:

    python3 apex_dump.py https://live.apex-timing.com/kip-palmela/ --seconds 120

It writes a `.raw` of every frame and a `.json` saying which columns were
recognised, which were ignored, and whether the pit counter and race clock came
through. Replay one later with `--replay <file>.raw`.

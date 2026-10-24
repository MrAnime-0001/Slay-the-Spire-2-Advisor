# StS2 Advisor: dev log

Newest first. Each entry lists what was added, which game build it was made on, and where the details live (`.planning/`).

**Game builds:**
- **v0.107.1** (Steam default branch): the current release. Everything up to v12 was built and tested on it.
- **v0.111.0** (Steam beta branch): supported since v13, side by side with v0.107.1.

When the beta becomes the release, nothing is removed:
- The reader tries the old member paths first, then the new ones.
- Each build keeps its own data file (`advisor/gamedata/<version>.json`).
- Tests run against both builds.

Version history is kept here and in `.planning/*.md`. The code itself is not under version control yet.

---

## Overlay + autopilot + solver (2026-10-25): auto-update, late pick screens, Sandpit race
- **Auto-update:** the overlay watches its own source (`advisor/*.py` except tests, `reader/*.cs*`).
  - Restart happens once files are quiet for 15 s (`UPDATE_QUIET`), every `.py` compiles, and autopilot isn't running.
  - Then it exits with code 3. `run.bat` loops on 3: closes the reader, rebuilds it, relaunches.
  - A file that doesn't compile shows "update not loaded" and keeps the old code running.
  - The window position is saved to `advisor/overlay_pos.txt` and restored at launch (`STS2_POS` still overrides).
- **Header** says "by Yutti" instead of "unofficial".
- **Autopilot, late pick screen:** Colorless Potion's Choose a Card opened after the potion left the bar, while Solar Strike was being aimed. Card keys and enemy hovers do nothing under that screen, so the 90-point sweep failed: "couldn't find HunterKiller on screen".
  - New `picking(st)`: before a card key, during the sweep (`gone`), after a failed play and in `missed()`, an open pick screen ends the play with no error. The loop handles the screen and replans.
  - The enemy's known spot is no longer dropped when an aim is interrupted.
- **Solver, The Insatiable's Sandpit is a race** (321/341 HP vs a countdown of 4, 6 Frantic Escapes). Before, an Escape was only worth `12 / turns left`, so the advisor attacked until the last turn and died with Escapes unplayed.
  - New term: HP the pit owners still have when the countdown ends, minus our deck damage per turn x turns left, is a loss (`SANDPIT_RACE`). An Escape is worth a turn of our damage while the race is short.
  - Audit of other clocks in the decomp: Battleworn Dummy, Escape Artist (Thieving Hopper) and Hatch already have kill-now weights. Waterfall Giant's eruption, Gas Bomb's death blow and The Gambit are already modelled. No monster puts Doom on us. Strength-scaling enemies are in the forecast mean. The Sandpit was the only uncovered one.
- Tests 272 (late pick screen) and 273 (Sandpit race), both builds.

## Reader + overlay (2026-10-25): runs know who they are (seed)
- **Reader `seed`** (`RunState.Rng.StringSeed`, e.g. CZP517EFR0ST, the code shown in-game) in combat and run states. It's the same id the game's run history stores, so a live run, its recordings and its finished `.run` file line up.
- **Recordings** carry the seed. After an advisor restart, the first state isn't written twice: the dedupe key is read back from the file's last line.
- **Same run vs new run:**
  - Restarting the advisor mid-run continues from game memory; nothing to merge.
  - `tools/learn.py` rebuilds from the history files on every run (one file per finished run, keyed by start time), so a run is never counted twice. A new run is a new file. Abandoned runs are skipped.
- The reader compiles. Restart `run.bat` to rebuild it.

## Learning L1 (2026-10-25): the advisor learns from every finished run
- **`tools/learn.py`** reads the game's own run history (`%APPDATA%/SlayTheSpire2/steam/<id>/profile*/saves/history/*.run`): every player's final deck and relics, yours and teammates', plus every card offer and pick.
  - Per character it writes `advisor/learned.json`: decks with a card / relic, wins, offered, taken, and a win-rate lift shrunk toward 0 (`SHRINK` = 10 pseudo-runs at the base rate).
  - First run: 100 finished runs. Defect 24 decks / 42% wins, Ironclad 40 / 28%, Necrobinder 55 / 42%, Regent 38 / 42%, Silent 33 / 36%.
- **Picker:** `learned()` adds `LEARN_WEIGHT` x lift, capped at +/-3 (`LEARN_CAP`), to card ratings and relic values. It's added before the dilution guard, so past runs can't lift a card the deck sim says dilutes.
  - Example: Compile Driver -14% for Defect (the advisor wanted it on F6 of the won run); White Noise +14%.
- **Overlay:** on start, re-learns in the background when a newer run file exists, then reloads `picker.LEARNED`. So each run teaches the next.
- `tools/runreview.py` stays the per-run comparison (advisor vs your choices). Shop offers are now excluded from its agreement count.
- **Caveats:** win correlation isn't cause (strong decks take more cards); hence the shrink, the cap and the dilution guard. Old builds' cards count only if the name still exists in some gamedata build.
- **Tests:** 271. Tests set `STS2_NO_LEARN=1` so picks stay deterministic.

## Picker R1 (2026-10-25): engine cards valued by their every-turn output (lessons from a won A3 Defect run)
- **Review:** `tools/runreview.py <history .run> <steam id>` replays a finished run's rewards and upgrades through the picker (the game's own run history). On the 2026-10-25 win the advisor agreed on 12/22 rewards. The player picked and upgraded what grew the deck's output (Machine Learning, Defragment, Capacitor, cost-0 White Noise) and skipped in Act 3, while the advisor chased printed numbers.
- **`engine_value`** (picker.py) reads each card's own text and ops for lasting per-turn effects:
  - per-turn channels (Spinner, Lightning Rod's next N turns): the orb's passive each turn
  - Focus: x orbs held (3 + Capacitor / Modded slots) in an orb deck
  - orb slots: half a Lightning passive each
  - extra draws: the deck's average card
  - evoke triggers (Thunder): ~0.7 evokes a turn
  - the solver's `ENGINES` table
  - Worth x `POWER_TURNS` (4) in `rate`; Powers are no longer valued by their printed vars.
- **Upgrades:** known engines get the real output change through `rate`; the flat +4 per Power upgrade is kept only for unknown ones. Defragment+ (+1 Focus) now scores 20; Loop+ (2 triggers) scores 12.
- **Act 3 skip bar:** +3 (`LATE_SKIP`) once the deck is committed or 25+ cards.
- **Result:** 13/22 agreement on that run. Upgrades now go to output (Defragment, Loop). Spinner vs Machine Learning is still a judgement call; L1 (learning from all 120 runs) will calibrate it.
- **Tests:** 270 (both builds).

## Icons I1 (2026-10-25): every intent, the beta's Underworld power
- **Intent icons:** the overlay mapped only attack / debuff / buff / defend, and "CardDebuff" matched debuff. `icons.INTENTS` / `intent_icon` now map all 13 kinds the reader reports to the game's own icons: attack, buff, debuff, card_debuff, defend, escape, heal, sleep, status, stun, summon, death_blow, hidden, else unknown. Plan rows show them next to the target.
- **Beta-only powers** (Underworld) live in `images/powers/beta/`. Power lookups now try `images/powers/` and `images/powers/beta/` after the atlas.
- **Self-check** (`python advisor/icons.py`): relics 300/300, potions 64/64, powers 244/244, UI/energy/character/room/intent/orb 43/43. Placeholder classes (Deprecated*, VakuuCardSelector) are skipped. All icons come from your own game files; no download.
- **Not changed:** Ancient map nodes keep the generic icon. Each Ancient has its own map icon, and the reader doesn't know which one a node holds.

## Audit A5 (2026-10-25): curses priced by what they do
- `picker.CURSE_COST` (event scale; a plain dead draw = the old flat 8):
  - Bad Luck 16 (13 unblockable HP a draw)
  - Normality 14 (3-card cap)
  - Regret 12 (HP = hand size)
  - Enthralled 10 (2 energy, played first)
  - Decay / Doubt / Shame / Writhe / Folly / Poor Sleep / Debt 9 (turn-end damage, a debuff, clogging)
  - Spore Mind 5 (can be played away)
  - Guilty 4 (leaves after 5 combats)
- Used for event options that add a curse (`card_value`) and for removal order: the costliest curse goes first, Guilty last.
- Test 223 changed: Regret is now worse than Doubt (they used to be equal). Test 269 (both builds).

## Audit A4 (2026-10-25): effects that leave with their source, forecast gaps
- **Kill the source, lose the effect** (each power's AfterDeath):
  - Constrict ends when Slithering Strangler dies.
  - Shrink ends when Shrinker Beetle dies (later attacks deal full damage).
  - Hex's Ethereal ends when Spectral Knight dies.
  - Galvanic ends with Globe Head (A2).
  - The Lost / The Forgotten give back the Strength / Dexterity they stole from us the moment they die. The reader exports our stolen amount from the Possess power's per-player dictionary.
- **Vulnerable an enemy puts on us mid-turn** (Queen, Soul Nexus, Ovicopter, Flyconid, Mawler, Owl Magistrate, Soul Fysh, Terror Eel, Test Subject): enemies acting after it hit x1.5.
- **Surrounded:** a kill that leaves every enemy on one side turns us to face them. The back-attack x1.5 no longer stays after the kill.
- **Paper Cuts / Painful Stabs** count only their owner's unblocked hits; it used to be the max over enemies, applied to all hits.
- **Personal Hive:** a Dazed for every powered attack hit, blocked or not, Osty's included.
- **Forecast:**
  - The Obscura's Sail (+3 Strength to its whole side) now counts for itself and its teammates' later hits.
  - Summons (Ovicopter's eggs, Two-Tailed Rat, Living Fog's Gas Bomb...) add their expected hits to the summoner's later turns, so killing the summoner first is valued.
  - New Withers use the upgraded Wither damage.
  - Drawing Void costs about one average card (lose 1 energy).
- Battleworn Dummy (timer 1) and Thieving Hopper (Escape Artist 1): kill-now weight, like Hatch.
- **Not changed, on purpose:** heals (Knowledge Demon, Waterfall Giant) don't change this turn's best play. Queen's ally Strength amount is a runtime value (counted 0); Enrage / Ravenous later Strength, The Forgotten's Dex in Dread, Flutter's countdown and Suck per hit stay ponytail notes.
- **Tests:** 268 (both builds). The reader compiles.

## Audit A3 (2026-10-25): made cards and Sly auto-plays follow the same limits
- **Cards made this turn** (Shivs, potion picks...) get the affliction an active power gives cards entering combat (`AfterCardEnteredCombat` in the game code):
  - Ringing / Hex on us: any card.
  - Tangled on us: Attacks (Entangled, +cost).
  - Globe Head's Galvanic: Powers.
  - Infested Prism's Vital Spark: Skills (Tainted).
  - Before, made cards had none: Shivs after a Ringing Blade Dance, or cheap Shivs under Tangled, were planned but the game would refuse them.
- **Sly auto-plays** go through `allowed()` (Normality, Sloth, Ringing, Smog, Bound). Only Enthralled lets auto-plays through, as in the game.
- **Normality / Enthralled limits** stop once that card is discarded or exhausted.
- **Tests:** 267 (both builds).

## Audit A2 (2026-10-25): game rules the solver had wrong
- **Galvanized** (Globe Head): Power-card damage is Unpowered damage, so Block takes it first, and it stops once Globe Head is dead. It was HP loss.
- **Crab Rage** (Crusher / Rocket): the survivor gets +99 Block the moment its partner dies (+6 Strength was already modeled). The plan now hits the big crab first, then kills the small one.
- **Discard / exhaust picks:**
  - Playable junk (Toxic, Slimed) is now ranked by its turn-end harm, like unplayables. Toxic was kept, because its Damage 5 read as attack value.
  - Frantic Escape is no longer tossed as junk.
  - Doubt / Shame rank above Dazed.
- **Regret:** cards left in hand = unplayables left + unplayed cards. Made cards played (Shivs, copies) used to drive the count to 0.
- **Slumber:** wakes only once enough hits get through Block (one per hit), not on any damage. Slumber 1 wakes at its own turn end anyway, so hitting it then costs nothing.
- **Hard to Kill:** caps every hit (orbs, relics, Thorns), via enemy row slot [13] in `hit()`. Thorns damage back is now dealt per hit.
- **Doubt / Shame held at turn end:** cost a quarter of next turn's attack / block output (Weak / Frail 1 on us).
- **Tests:** 266 (both builds).

## Audit A1 (2026-10-25): win check + enemy per-turn state across replans
Plan: `.planning/enemy-effects-audit.md`, from three game-code audits of every enemy effect, status and curse.
- **Win check.** `REVIVE_POWERS` lists only the powers whose `ShouldStopCombatFromEnding` is true. Illusion and Reattach moved to `NO_END_POWERS`. Lethal turns vs Fogmog / The Obscura / Decimillipede now score the win.
  - A Decimillipede segment at 0 while another segment lives is not a kill (it reattaches, healing 25), and still counts as a later threat.
  - The same goes for an Illusion while a leader lives (it revives at full HP).
- **Reader `data` per power** (`PowerModel._internalData` bool/int/decimal fields, plus Withering Presence's `CardsLeft`):
  - Hardened Shell: the cap left this turn = Amount - damageReceivedThisTurn. The old reader sent the full amount, so replans overestimated damage.
  - Skittish: no second Block once `hasGainedBlockThisTurn`.
  - Withering Presence: Withers come from its real `CardsLeft`, which carries over between turns. It was assumed to restart at 0.
- **Reader `skills_played`** (Smoggy): one Skill per turn also holds after a replan, even for Skills made after the first one (no Smog on them).
- Older reader output keeps the old behaviour. Tests 265 (both builds). The reader compiles.

## Solver + reader (2026-10-25): Vulnerable carry-over, Buffer per hit, Bound across replans
- **Vulnerable carry-over (`vuln_later`).** Enemy Vulnerable ticks down at the end of its turn (game code), so Vulnerable v left after the plan boosts our next v - 1 turns. Credit per turn = deck damage per turn / living enemies x the Vulnerable bonus (Paper Phrog, Cruelty, Debilitate), discounted per turn. On a quiet turn a Vulnerable card now beats Defend.
- **Buffer per hit.** `enemies.predict` adds `hit` (the expected biggest single hit) to each forecast turn. `potion_price` values Buffer (Lucky Tonic) at min(unblocked damage then, that hit), not the whole turn. Example: before a 3 x 8 turn, stopping today's 8 is worth the same, so it no longer waits. Intangible still counts the whole turn.
- **Bound (Chains of Binding).** Per the game's code, one Bound card per turn. Dupes don't count. Sly auto-plays are checked too (`CardCmd.AutoPlay` calls `ShouldPlay`): a discarded Bound Sly card stays unplayed once one was played, and if it auto-plays first it uses up the turn's Bound play.
  - Reader: `bound_played` (your non-dupe Bound card plays this round, from combat history). After a replan the advisor no longer suggests a 2nd Bound card.
  - Solver: a `BoundPlayed` marker in buffs, set before the card resolves (`BeforeCardPlayed`), so it covers hand plays and Sly auto-plays.
- **Tests:** 264 (both builds). Test 263's Lucky Tonic case is now "drink now" (the 3 x 8 example above). The reader compiles.

## Solver (2026-10-25): plans around the enemy's coming turns (Weak, potion timing)
- **Request:** save potions and debuffs for the enemy's big turn; attack big when it isn't attacking.
- **Enemy Weak carries over.** Forecasts ignored Weak. Weak ticks down at the end of each enemy turn (game code), so Weak w left after the plan now cuts forecast turn k while w > k + 1. A Weak card on a quiet turn now counts for next turn's big hit. Old: Defend on a turn with nothing incoming.
- **Potion opportunity cost (`potion_price`).** Each drunk potion costs `max(pot_cost, what it would do later)`:
  - Block / Buffer (Lucky Tonic) / Intangible (Ghost in a Jar) / Weak: what they save on the forecast's biggest enemy turn, if it beats this turn's hits. A Weak drunk now that still lasts until then gets no hold value; `future_left` credits it.
  - Flex / Speed (this turn only, `TEMP_POTIONS`): an average hand's Attacks / Block. They wait for a hand with more Attacks or Block to boost.
- **Strength / Dexterity Potion now last the fight in `lasting()`.** Before, they were valued for one turn only, so they were hoarded. Now they're drunk early when it pays.
- **Attacking on quiet turns:** already came from Block being worth ~0 with nothing incoming. With the engine fix (above), quiet turns also go to Powers.
- **Tests:** 263 (both builds). It fails on the old logic: Defend, Block Potion / Lucky Tonic drunk on a small hit, Strength Potion kept.
- ponytail: Buffer is counted as stopping the whole spike turn. Vulnerable carry-over (our next turn's damage) isn't modeled yet.

## Overlay (2026-10-24): orb row matches the game
- Orb icons now run newest to oldest, left to right. The oldest orb (the next one evoked) sits on the right, like the game's orb ring. The data order is unchanged; only the display is reversed.

## Solver (2026-10-24): Powers on a quiet turn, teammate debuffs, own card count
- **Report:** Knowledge Demon boss, multiplayer, its turn not an attack. With Capacitor and Loop in hand, the plan was Defend + Defend.
- **Cause 1 (all engine Powers):** `from_ops` moves a Power's amount from `vars` into `op_buffs`, but `engine_powers` still read `vars`. Every `ENGINES` power played through ops was worth 0 for later turns (Loop, Noxious Fumes, Genesis...). With nothing incoming, Defend tied and came first.
  - Fix: `engine_powers` reads `op_buffs` for ops cards.
- **Cause 2:** Capacitor / Modded (+orb slots) and Bulk Up (-1 slot) had no value. Their `orb_slots` op is now the var `SlotsGained`. `lasting()` counts it as half a Lightning passive (+Focus) per slot per later turn (ponytail: no orb types read).
- **Cause 3 (enemy forecast):** a teammate's Enfeebling Touch (-11 temporary Strength) was kept forever, so later turns predicted -9 damage. Next Slap showed 6 instead of 17.
  - Per the game's code, `TemporaryStrengthPower` ends at the end of its owner's turn. `enemies.predict` now adds it back after the shown move (`TEMP_STRENGTH_DOWN` / `_UP`, same lists in both builds).
  - Each hit is now floored at 0.
- **Cause 4 (multiplayer):** `cards_played` counts every player's cards. Sloth, Velvet Choker and Brilliant Scarf count yours only. The reader adds `my_cards_played` (your creature, from combat history). The solver uses it, except for Slow (Bygone Effigy), which counts all cards.
- **Recorded state:** now plays Capacitor + Loop and keeps 1 energy (Pael's Tears).
- **Tests:** 261-262 (both builds). The reader compiles. Restart `run.bat` to rebuild it.

## Reader + solver (2026-10-24): turn counters survive an advisor restart
- **Question:** if the advisor is closed and relaunched mid-fight, does it forget where the enemy is in its move cycle?
- **Already live (no change):** each poll reads from game memory, not from advisor history:
  - enemy next move (`NextMove.StateId`) and its last 3 moves (`MoveStateMachine.StateLog`)
  - turn number, cards played this turn, powers, relic counters
- **Fixed:** two "this turn" counts were tracked only inside the advisor, so a restart reset them:
  - Cards exhausted this turn (Forgotten Ritual on v0.107.1, and others that need it). It was counted from the exhaust pile's growth since the advisor first saw the turn.
  - Attacks played this turn (Juggling copies the 3rd). It was assumed to be 0 whenever cards had been played before the replan.
- **Now:** the reader counts both from the game's combat history (`CardExhaustedEntry` / `CardPlayStartedEntry` of the current round, your own creature only). The fields are `exhausted_this_turn` and `attacks_played`. Both builds have these entries. The old fallbacks stay for older reader output.
- **Tests:** 260 (both builds). The reader build compiles. Restart `run.bat` to rebuild it.

## Overlay (2026-10-24): why the plan keeps energy
- **Report:** with 1 energy left (Defect, multiplayer), the plan said "End turn" while Strike / Defend / Barrage were affordable.
- **Cause:** Pael's Tears. Per the game's code, ending your turn with any unspent energy gives +2 energy next turn, every turn. Holding 1 energy is a net +1, so the solver chose to keep it. Checked on the 3 recorded states: without Pael's Tears it plays Defend / Strike / Barrage.
- **Change:** when the plan leaves energy while a card is still affordable, the overlay now says why:
  - with Pael's Tears: "keeps 1 energy on purpose: Pael's Tears gives +2 energy next turn"
  - otherwise: "no card adds more than it costs this turn"
- **Re-planning:** the overlay already reads the game every 100 ms and re-plans on any change, including:
  - energy, hand or draw pile
  - your HP or Block
  - enemy HP, Block or powers
  - intents
  - anything a teammate does
- Test 259.

## Fix (2026-10-22): Living Fog's Smog, and Esc opening the pause menu
- **What happened:** against Living Fog (Smoggy), autopilot played Battle Trance, then tried Defend twice. It stopped with "DefendIronclad didn't play", and the game's Pause menu was open.
- **Smog rule, from the game's code:**
  - under Smoggy, your first Skill puts Smog on every Skill
  - a Smog card can't be played at all
  - so it's one Skill a turn
- **The bug:** the solver allowed one more Smog Skill. After the replan, Battle Trance no longer counted. Now any Smog card is unplayable, and with Smoggy only one Skill fits in a plan. Test 63 encoded the old reading; it's corrected, plus test 63b for this exact case.
- **Pause menu:**
  - after a failed play, autopilot pressed Esc to put the card back
  - when the reader can't find the hand node it reports `holding` as unknown, and autopilot then pressed Esc anyway
  - with nothing held (an unplayable card is never picked up), Esc opens the pause menu
  - now Esc is pressed only when the reader confirms a held card (test 258)

## Fix (2026-10-22): autopilot stopped after Colorless Potion
- **What happened:** F8 autopilot drank Colorless Potion as the last step, found nothing left to play, and pressed End Turn. The potion's "Choose a Card" screen opened a moment later, so E did nothing and autopilot stopped with "end turn didn't register".
- **Fix:** if a pick screen (Choose a Card, hand select, pile pick) opens while autopilot waits for the turn to end, it now handles the pick and ends the turn again (test 257).
- **Overlay:** in-combat "choose a card" rows used to show raw plan scores (0.0 / −9.0 / −24.0), which only mean something relative to each other. They now read "best plan" and "−9.0 vs best".
- In the reported case (Eternal Armor / Production / Mind Blast vs a 3×3 hit at 0 energy), the ranking was already right. A free Eternal Armor's 9 Plating blocks all 9.

## P9 (2026-10-22): leftovers
- **Know Thy Place:** debuffs now land in the card's own order (from the game's code). Its Weak comes first. Against an enemy with 1 Artifact, the Weak is blocked and the Vulnerable lands.
- **Parry:** Sovereign Blade also gains Block equal to your Parry amount (Dexterity applies), per the game's code.
- **Pael's Eye:** if it's unused this combat and you play no card all turn, your hand is Exhausted and you take an extra turn, so the enemy's hit waits. The advisor now sees that. Example: a 30 hit coming and only a Strike in hand means play nothing.
- **Tinker Time:**
  - Weapon / Protector / Gadget is picked by what the run plan lacks most: AoE → Attack, Block → Skill, scaling → Power.
  - The riders get rough values. Violence, Wisdom, Choking, Expertise, Sapping, Chaos, Curious and Energized all score; Improvement is scored by the existing "Upgrade a random card" rule.
- Tests 253-256.
- **Final checks:**
  - Replay: 710 turns, mean error 2.2.
  - `tools/hp_audit.py`: 5998 turns. Avoidable damage in 39 turns (0.65%). Before v19 it was 38 of the same kind.
  - Predicted HP loss per turn: hallway 2.65, elite 4.21, boss 5.04.
  - The avoidable cases left are almost all Inflame played for its Strength over the fight (4-5 HP in Terror Eel / Gardener elites and one Exoskeletons hallway fight). Lower Inflame's later-turn value if that looks wrong in play.

## P8 (2026-10-22): wiki pages missing from the scrape, fetched
- **What was fetched:** the pages listed in `MISSING.md` (your approval, 2026-10-22), from slaythespire.wiki.gg, with your own `_scripts/crawl.py` (`get_page`). No other pages.
  - 120 distinct titles, 119 fetched. "Disintigration" is a misspelled link with no page.
- **Conversion:** your `_scripts/convert.py` → `Research/_missing_out/`.
- **Merge:** new pages appended to `data.json` (1256 → 1375). The old file is kept as `data.before-missing.json`. The .md pages were copied into their folders without overwriting anything.
- **Re-import:** `advisor/wiki.json` now has 472 cards (+20), 198 relics (+19), 20 potions (+5).
- **Ascension page:** matches the game code (`.planning/research/code-facts.md`). That file's advisor-handling notes were updated: A3 gold, A7 odds (P7) and A10 double boss are all modeled.
- Everything stays local (community text), like `icon_cache/`.

## P7 / W9 (2026-10-22): card reward odds from the wiki
- **Card reward value** on the map now uses the wiki's odds ("Cards" page) instead of a flat number:
  - each of the 3 cards is rare 3% after a normal fight, 10% after an elite (halved at A7+)
  - reward cards come upgraded 0% / 25% / 50% in Acts 1 / 2 / 3 (halved at A7+)
- Value = 4 + 8 × chance of a rare in the reward + the chance of an upgraded card × half an upgrade.
- **What that means:** an elite at A0 is worth about what it was before (relic 12 + about 6.2). At A7+ its card edge shrinks. Later acts make every fight's reward slightly better.
- **Not added:** odds for "?" rooms and shop prices. The wiki pages are missing (P8 re-scrape), and shop prices are read from the game.
- Test 252.

## P6 / v19 Phase D (2026-10-22): map routes scale with your deck and potions
- **Deck strength:** fight costs on the map are scaled by how strong your current deck is.
  - It uses the deck sim (`deck.hp_lost`) compared with your recorded fight costs for the act.
  - The sim runs about 2.4× your real losses (median over 104 recorded decks), so the factor divides by that and is clamped to 0.6-1.5.
  - A strong deck makes elites look cheaper; a weak one makes them look more expensive.
  - Rest-site advice uses the same costs.
  - Only when the act is known (the sim needs the act's enemies).
- **Potions:** each potion you carry (max 2) takes 8 HP off the bad case of the next Elite or Boss, and half that off the expected loss. They count as drunk there, so later fights don't get them again.
- **Rough calibration:** one bias for all acts (Overgrowth ≈ 2.6×, Underdocks ≈ 1.1-1.8×). Per-act numbers if routes look off.
- Tests 250-251.

## P5 / W6 rest (2026-10-22): events that showed "?"
- **Trial:**
  - Accept is worth the average of the best verdict on each of the three possible verdict pages (Merchant / Noble / Nondescript, from the game's text). Reject leads back to Accept, so it scores the same.
  - Double Down kills you and scores −1000.
  - The verdict texts now parse: "Upgrade 2 cards", "Obtain 300 Gold", "Gain 2 card rewards".
- **Relic Trader:** "Trade X for Y" is worth the new relic's value minus the one you give up, with the run plan.
- **Crystal Sphere:** "Gain a Debt" is scored as a curse. Divining is not scored.
- **Tea Master** (rough values): Bone Tea ≈ half an upgrade, Ember Tea ≈ 1.2 per fight with Strength, Tea of Discourtesy −1 per Dazed.
- **Not added:** Tinker Time's riders still show "?". They are follow-up choices on one custom card. Ancient relic pools don't affect map routes: an Ancient room is at every act start, whatever path you take.
- Tests 247-249.

## P4 / W5 rest (2026-10-22): more enemy powers, wiki Pattern audit
Each was checked against the game's code:
- **Nemesis:** the enemy turns Intangible at the end of every other turn. If it isn't Intangible now, it will be on your next turn, so damage to it now counts 1.5×.
- **Hatch:** an egg with 1 turn left hatches after its next turn. Damage to it now counts 2×.
- **Suck:** each enemy attack that gets past your Block gives it Strength. That is counted as future HP lost for 2 turns of its hits, which makes full blocks worth more.
- **Asleep (Lagavulin Matriarch) / Slumber (Slumbering Beetle):** damage that gets through wakes them early. Chipping a sleeper without killing it costs about 8 HP of future damage. Example: a lone Strike into a 60-HP sleeper is no longer played.
- **Rampart:** the Turret Operator's Block lands at the start of your turn, so it is already in the Block the game shows.
- **Rough constants:** NEMESIS_NOW 1.5, HATCH_NOW 2, SUCK_TURNS 2 and WAKE_COST 8 are first guesses. Tune them from recorded fights.
- **Audit:** `tools/import_wiki.py` now compares the wiki's Pattern damage numbers with the game's attack values. 14 differences, mostly wiki pages that cover several monsters (Queen and her minions, Turret Operator and turrets). Vantom (game 26, wiki 27) may be a version difference. The game's data still wins; nothing changes automatically.
- Test examples for unmodeled powers changed (Hatch is modeled now). Tests 244-246.

## P3 / W4 rest (2026-10-22): powers that trigger on cards, debuffs and draws
Each was checked against the game's code.
- **On an enemy, per card you play after it lands:**
  - Strangle: the enemy loses 2 HP; Block doesn't stop it.
  - Oblivion: +3 Doom. **Bug fix:** the Oblivion card used to count as 3 Doom right away. It gives 3 Doom per card played after it.
- **Your powers:**
  - Sleight of Flesh: damage each time one of your debuffs lands (not when Artifact blocks it).
  - Panache: every 5th card hits ALL.
  - Haunt: a Soul played makes a random enemy lose HP.
  - Speedster: damage to ALL per card drawn.
  - Corrosive Wave: Poison to ALL per card drawn.
  - Smokestack: damage to ALL per Status you create.
  - Black Hole: damage to ALL when you spend or gain stars.
  - Necro Mastery: when enemies hit Osty, every enemy loses that much HP.
  - Soulbound: each Soul you make adds more Souls to your draw pile (counted as value kept for later).
- **Bug fix (Haunt):** the Haunt card's HP number used to be read as HP you lose.
- **Engine values:** playing Speedster, Panache, Black Hole, Smokestack, Necro Mastery, Sleight of Flesh, Haunt, Prep Time, Machine Learning, Spectrum Shift, Genesis, Loop or Well-Laid Plans now counts their later turns. The per-turn numbers are rough guesses, for example about 5 draws a turn for Speedster.
- **Temporary Strength** (Piercing Wail, Crush Under, Dark Shackles, Enfeebling Touch, Shackling Potion, Flex Potion, Setup Strike): already part of the Strength the game shows, and gone at the end of its owner's turn. The overlay no longer lists these as "not modeled". The same goes for Noxious Fumes and Neurosurge, which act at the start of your turn.
- **Wiki "Order" notes** for cards in your recorded fights:
  - Hemokinesis and Breakthrough lose HP *before* they hit, so Rupture's Strength now boosts their own damage.
  - The other notes (Fight Me! Strength after its hit, Weak after a Strength loss) were already right.
  - Know Thy Place's Weak-before-Vulnerable order against exactly 1 Artifact is not handled.
- `tools/import_wiki.py`: the audit now counts engine powers as modeled. Still unmodeled: Hatch, Nemesis, Rampart, Parry, Suck, Slumber and a few rare ones.
- Test 77's example changed to Automation (Well-Laid Plans is known now). Tests 237-243.

## P2 / W7c (2026-10-22): relics that act during your turn
Each was checked against the game's code (both builds, the same counter fields). Before this, the overlay listed all 18 under "relics not modeled".
- **Free cards:**
  - Brilliant Scarf: the 5th card each turn is free.
  - Mummified Hand: a Power makes a random card in hand free (counted as about 1 energy).
- **Card cap:** Velvet Choker stops you at 6 cards a turn.
- **Enemy turn:**
  - Diamond Diadem: playing 2 or fewer cards halves the enemy's hits.
  - Undying Sigil: an enemy whose Doom is at least its HP hits for half.
- **Draws:** Game Piece (draw on each Power) and Iron Club (draw on every 4th card, using the live counter) add to the plan's draw value. Pocketwatch (3 or fewer cards: draw 3 more next turn) is valued the same way.
- **Next turn:** Art of War (no Attacks this turn: +1 energy next turn), valued like Pael's Tears.
- **Damage and Block:**
  - Mystic Lighter: Enchanted Attacks +9.
  - Vitruvian Minion: Minion cards ×2 damage and Block.
  - Pael's Legion: the first card Block is doubled while it is awake.
  - Helical Dart: each Shiv gives +1 Dexterity this turn.
  - Rainbow Ring: Attack + Skill + Power in one turn gives +1 Strength and +1 Dexterity, after that card resolves.
- **Copies:** Music Box puts an Ethereal copy of your first Attack in your hand.
- **Unsettling Lamp:** your first card that debuffs an enemy gets its Vulnerable / Weak / Poison / Doom doubled.
- **Razor Tooth:** upgrades the card you play; that does nothing more this turn.
- **Pael's Eye (extra turn):** still not modeled.
- Tests 231-236.

## P1 / W7b (2026-10-22): potion swaps
Plan for the remaining work: `.planning/remaining-work.md` (P1-P9).
- **Rewards screen:** when every potion slot is full and a reward potion is worth at least 1.5 more than your worst one, the overlay says "Swap: drop Speed Potion for Beetle Juice". You discard the old potion first, then take the new one.
- **Shop:** with full slots, the best such potion can still go in the basket, labelled "(drop X)". It counts only for what it adds over the potion it replaces. One swap per visit.
- Potion values are unchanged (`potion_value`: Mobalytics tier by act, plus build fit).
- Autopilot (F9, parked) still leaves potions when slots are full.
- Tests 229-230.

## W7 part 1 (2026-10-22): relics and removals follow the build plan
- **Key relics:** a key relic of the build the deck is going for is worth +6 (+3 while Act 1 is still open). Example: Snecko Skull in a committed Poison deck goes from 11 to 20. Kunai in the same deck stays at 15.
- **Wiki relic notes:** the relic's Synergies / Anti-Synergies against cards in the deck count the same way as for cards: +1 each (max +3), −2 each. Example: Velvet Choker with Leading Strike in the deck.
- This applies wherever relics are scored: shop, treasure chest, event and Ancient options (Neow included). The treasure rows show the reason ("key relic: Poison, synergy x3").
- **Shop removal:** removing a Basic card is worth +3 × commitment once the deck is committed, because a thinner deck draws the build more often.
- `runplan.roles()` tolerates card dicts without a type (old test decks).
- Tests 227-228.
- **Still to do (W7 part 2):** potion swap advice when slots are full, and the 18 in-combat relics the solver doesn't model yet.

## W6 part 1 (2026-10-22): events follow the build plan
- **Named cards:** "Add X to your Deck" used to be worth a flat 4. Now the card is rated like a card pick, with the run plan, minus the skip bar (same as the shop). In a Poison deck, Deadly Poison scores +8 and Blade Dance −4. A forced off-build card is worth less than nothing. Curses keep −8.
- **Wood Carvings:** "Transform a starter card into Peck / Toric Toughness" = removing a Basic + that card's rating.
- **Named relics, potions and cards** from the event's vars or text ("Receive {RelicName}", "Obtain the Sword of Stone", "Procure Glowwater Potion", "Receive the Spoils Map") use `relic_value` / `potion_value` / the card rating, with the run state.
- **Fixes:**
  - "Downgrade a random card" now costs instead of scoring "?"
  - "Enchant 1 card" now parses
  - "Obtain 3 Defect Cards" (capital C) now parses
- **Wiki fallback:** when the game text doesn't parse, the wiki's text for that option is scored instead and shown on the overlay (for example Brain Leech "Choose 1 of 5 random cards").
- **Still "?":** Trial, Tinker Time, Crystal Sphere's Debt, Relic Trader, Tea of Discourtesy. Ancient options (Neow and the others) are already scored as relics; how well a relic fits the build is W7.
- Recordings hold only combat states, so these checks use the game's own event text.
- Tests 223-226.

## W8 / v19 Phase B (2026-10-22): card picks follow a build plan
New `advisor/runplan.py` works out which build the deck is going for and what it still lacks. Card picks and the overlay both use it.
- **Target build:** your character's strongest build by core cards and key relics (`archetypes.commitment`).
- **When it commits:** at 40% (2-3 core cards) or with a key relic. From Act 2 on, 20% is enough. Before that, Act 1 stays open: build cards get half bonuses.
- **Gaps:** no AoE, too little Block (under 30% of the deck), no real draw, no scaling (weighs more each act). Draw now uses the game's own "draws" flag, so Blade Dance (makes Shivs) doesn't count as draw.
- **Card score** (`picker.rate`, when the run state is known):
  - core card +6, build card +3
  - a card that fills a gap: up to +3
  - wiki Synergies with cards in the deck: +1 each (max +3); wiki Anti-Synergies: −2 each
  - once committed, an off-build card that fills no gap: −4 × commitment ("off-build: dilutes Poison")
- **Skip bar** rises by 2 × commitment, so a committed deck skips mediocre rewards more often.
- **Overlay:** each card reward row shows its reason ("core: Poison, synergy x1", "fills draw", "off-build: dilutes Poison"). F7 details show the plan: "Plan: Poison · committed 100% · needs: aoe, draw" or "Act 1: open · leaning Poison".
- Tests 219-222 (on Silent decks):
  - 3 Poison core cards commit the deck
  - Deadly Poison beats Blade Dance and Backflip
  - Dash / Slice: Skip
  - Dagger Spray fills the AoE gap

## W5 part 1 (2026-10-22): enemy powers, counter tips
- **High Voltage (Zapbot):** +2 Strength at the end of each of its turns, forecast like Territorial. The game's text says end of turn; the wiki says start, and the game wins.
- **Demise (Powdered Demise):** an enemy whose Demise is at least its HP dies at the end of its turn, so the advisor counts it as a kill (it still attacks first).
- **Counter tips:** in elite and boss fights, F7 details show the wiki's first "Useful Cards" note for each enemy type. Example, Phantasmal Gardeners: "Any attack which hits all enemies, such as Sow or Dagger Spray, can deal a significant amount of damage." The tip shows only if `advisor/wiki.json` exists (run `tools/import_wiki.py`).
- **Still to do (W5 part 2):** Nemesis (Intangible every other turn), Suck (Strength on unblocked hits), Hatch, Rampart; checking the game's AI against the wiki's Pattern tables.
- Tests 217-218.

## W4 part 2 (2026-10-22): per-card triggers, next turn, enemy card hazards
Picked by how often each power shows in your recorded fights:
- **Juggling:** a copy of your third Attack each turn goes to your hand.
- **No Draw (Battle Trance, Bullet Time):** draw cards do nothing for the rest of the turn, and the solver knows it. It no longer counts the "?" credit or the expected draws.
- **Next turn:** Energy Next Turn and Draw Cards Next Turn feed next turn's Block estimate, so the advisor knows a stronger hand is coming.
- **Withering Presence (Aeonglass):** every 6th card you play adds a Wither (3 damage at turn end if held), so the advisor weighs whether the 6th card is worth it.
- **Paper Cuts (Scroll of Biting):** an unblocked hit also costs 1 Max HP.
- **Painful Stabs (Test Subject):** an unblocked hit adds a Wound. Both make blocking those hits worth more.
- Test 61's examples of unmodeled powers changed (Paper Cuts and No Draw are modeled now). Tests 213-216.

## W4 part 1 (2026-10-22): engine Powers are worth their later turns
- **What changed:** playing an engine Power used to score a flat "?" credit of 2, about the same as a Strike's chip damage. The solver now values what it adds each later turn, over the fight's remaining turns (at most 6), discounted like other future value.
- **Powers covered:**
  - Noxious Fumes (ramps: its Poison stacks)
  - Inferno (minus 1 HP per turn)
  - Infinite Blades
  - Countdown (ramps)
  - Serpent Form
  - Envenom (ramps)
  - Lethality
  - Echo Form
  - Rolling Boulder (ramps)
  - Hailstorm
  - Unmovable, Fasten and Shroud (Block)
- **Long vs short fights:** with energy for only one card, Noxious Fumes beats a Strike against a 25+ HP enemy, and the Strike wins against 12-18 HP (the fight ends before Fumes pays off).
- **Still to do (W4 part 2):** effects that trigger when you play or draw a card (Sleight of Flesh, Strangle, Speedster, Smokestack, Black Hole, Haunt, Necro Mastery, Corrosive Wave, Oblivion); next-turn energy and draw; the wiki's card "Order" notes.
- Tests 211-212.

## v19 Phase A (2026-10-22): upgrades go to engines and key cards
You saw the advisor upgrade Strikes and Defends first. Causes and fixes in `picker.upgrade_gain`:
- **The tier blend damped real cards, not basics:** a card on the community tier list had half its upgrade gain hidden by the blend, while Strike and Defend (no tier entry) kept all of it. Upgrades are now compared without the blend.
- **The deck sim treats Vulnerable, Weak, Strength, Dexterity and Poison as on/off,** so Bash+ (Vulnerable 2 to 3) or Deadly Poison+ (5 to 7) gained nothing there. Their bigger amounts are now added.
- **Engine Powers:** an upgrade that raises a Power's own amount (Noxious Fumes 2 to 3 Poison per turn) was worth 0; it's now worth `POWER_UP_VALUE`.
- **Cost cuts** get `COST_UP_VALUE` per energy.
- **Basics count 0.3x:** they're the cards you remove, so they win only when nothing else gains. In a deck of only basics, Bash is first.
- **Same ranking in combat:** card-made upgrades (Armaments) now use the same ranking as the rest site. Rest-site lines mark basics "(basic: little gain)".
- **Example, Ironclad starter + Inflame / Pommel Strike / Shrug It Off:**
  - before: Inflame 3.0, Defend 2.6, Strike 1.8, Pommel 1.5, Bash 1.3, Shrug 1.2
  - now: Inflame 6.0, Pommel 2.1, Bash 1.6, Shrug 1.5, Defend 0.8, Strike 0.5
- Tests 208-210.

## W3 (2026-10-22): hazards
- **No Block (Panic Button):** Block from cards is 0, so the advisor stops counting Defends that do nothing.
- **The Gambit:** any unblocked attack damage counts as death, so the advisor blocks every hit or kills first.
- **Doom on you (Neurosurge applies 3 each turn):** if your HP after the enemy turn is at or below your Doom, you die. The plan blocks to stay above it.
- **Hand limit 10:** cards made or fetched mid-turn stop at 10 in hand (Blade Dance with 8 other cards adds 2 Shivs, not 3).
- Tests 204-207.

## W2 (2026-10-22): damage and Block multipliers (wiki Buffs/Debuffs)
- **Vulnerable:** Paper Phrog makes it +75%, Cruelty adds its % (+25), and Debilitate on the enemy doubles the bonus.
- **Weak on enemies:** Paper Krane makes it -40%; Debilitate doubles it too.
- **Attack multipliers:**
  - Tracking: Weak enemies take x2 from Attacks.
  - Double Damage: Attacks x2 this turn.
  - Gigantification Potion: the next Attack x3.
  - Lethality: the first Attack each turn +50%.
- **Colossus:** a Vulnerable enemy's hits on you are halved.
- **Block:**
  - Unmovable doubles the first card Block each turn.
  - Fasten adds its Block to Defend cards.
  - Shadowmeld doubles Block gained this turn.
  - Shroud gives Block whenever you apply Doom.
- **HP lost on your turn:** it lands after the card resolves (wiki), then Rupture gives Strength and Inferno hits ALL enemies.
- **Next turn's Block:** Block Next Turn and Crimson Mantle Block are added to next turn's Block estimate, so the advisor knows it can afford a lighter block now.
- **Buff order:** Strength, Gigantification, Double Damage and Vigor sources are tried first, so equal plans drink the potion before the attacks.
- **Not yet scored:** permanent powers (Cruelty, Tracking, Lethality, Unmovable, Fasten, Rupture, Inferno, Shroud) count once they're active. Their value when you play the Power card waits for the engine phase (W4).
- Tests 197-203. Replay: 710 turns, mean error 2.3.

## W0 (2026-10-22): wiki importer
- **New `tools/import_wiki.py`** reads your scraped wiki (`data.json`) and writes:
  - `advisor/wiki.json`: per game name, the wiki's Synergies / Anti-Synergies / Order notes (with the cards, relics and potions each note names), Notes and Spawn Restrictions for 452 cards, 179 relics and 15 potions; Useful Cards (counter-play) and Notes for 37 enemies; options for 56 events; relic pools for 8 Ancients (Neow's Curse and Positive pools...).
  - `.planning/research/wiki-audit.md`: wiki vs game numbers, unmatched pages, and the powers the solver doesn't model ranked by how often they show in your recorded fights (Soulbound, Withering Presence, Noxious Fumes, Neurosurge, Inferno, Juggling, Shroud, Energy Next Turn, Debilitate... lead).
- **Names come from the game:** pages are matched by the wiki's "Internal name" (e.g. `STRIKE_IRONCLAD`), then by the game's English title. Every name a note mentions is resolved to a real card/relic/potion of either build. Unmatched: only group pages (Bosses, Elites, Slimes...), Foul Potion, and 7 enemy pages.
- **Mismatches left:** 4 enemy HP values (Owl Magistrate 231 vs 234, Spectral Knight 93 vs 101, Scroll of Biting 30 vs 31, Axebot's formula). The game's data wins; these are likely version differences.
- `advisor/wiki.json` is community text: keep it local, like `icon_cache/`.
- **Rule from the wiki's Gardener page:** Skittish only triggers on Attacks. Damage from Skills (Flame Barrier, Noxious Fumes, Haze) lands without giving it Block. Fixed (test 195). Test 196 checks wiki.json's names.

## W1 (2026-10-22): wiki rules, combat correctness
You scraped the StS2 wiki (1256 pages, `Downloads/Slay the Spire 2 Research`). The multi-session plan for using it is `.planning/wiki-integration.md`. Gamedata from the game's code stays the source of numbers; the wiki adds rules, synergies, event outcomes and Ancient pools. This first pass fixes rules the solver had wrong:
- **Negative Strength:** a hit below 0 (e.g. Strike at -9 Strength) now deals 0. Before, it *added* Block to the enemy.
- **Enemy Intangible** turns a hit into 1 *before* Block: 20 into 5 Block takes 1 Block, no HP. Before, the Block was stripped first.
- **Strength, Weak and Vulnerable only change Attack damage.** A damaging Skill isn't boosted.
- **Poison is HP loss:** on an Intangible or Slippery enemy it ticks for 1, and Hardened Shell's cap counts it.
- **Artifact** also blocks enemy Strength loss (Piercing Wail-style cards) and Envenom's Poison, one stack per application.
- **Smog (Living Fog):** you can play one Skill, then no more that turn. It used to treat Smog Skills as never playable (test 63 updated).
- **Ritual applied by an enemy skips that turn's trigger:** a Cultist's Incantation is followed by Dark Strike 9, 11, 13. Before, the new Ritual was ignored entirely.
- Tests 189-194. Glass orb decay needs nothing: its passive fires at turn end, after the plan. Replay: 710 turns, mean error 2.3.

## v19a (2026-10-21): combat HP discipline, focus fire
You said hallway fights should cost about 0 HP, and that the advisor traded HP for damage too easily. Strategy research for this phase is in `.planning/research/strategy.md`.
- **Damage is worth the enemy turns it removes, not a flat rate.**
  - Damage is valued by how much HP those enemies would still take off you before they die: their forecast hits over the fight, less what your deck usually blocks, divided by your deck's damage per turn. The cap is 0.5 HP per damage point in hallway fights and 0.8 in elite and boss fights.
  - An enemy with no AI table counts as hitting 6 per turn.
  - The old `deck.exchange_rate` (up to 5 HP per damage point) and the tempo term are gone.
- **HP costs depend on the fight type:** hallway 1.5x, elite 1.25x, boss 0.75x.
  - Hallway HP costs the most: it turns later rest sites from Smith into Rest.
  - Boss HP costs the least because the act ends.
  - Potion cost scales the same way.
- **Future credits stay at the base rate or lower.** These are draw value, retained or top-decked cards, Dexterity, Plating and exhaust cards kept for later. They never use the 1.5x hallway rate because they are expected, not certain. In a boss fight they drop with boss HP, so a boss's Impervious is spent on a big hit instead of being saved.
- **Unknown draw pile:** next turn's Block is your deck's average Block per turn, not 0.
- **Certain draws:** a draw that takes the whole draw pile (Drum of Battle with 2 cards left) puts those cards in hand for this turn's plan. Exception: when a drawn card draws too (Soul drawing Soul), which looped forever.
- **Focus fire:** damage counts by an enemy's threat per HP left, so the scaler or the nearly dead enemy is finished first.
  - In the Phantasmal Gardeners elite, the first hit on a Gardener gives it 6 Block (Skittish), and that Block is gone at its turn.
  - The first hit each turn still goes to the one about to Enlarge. Leftover hits go to another Gardener when they would only hit the Block, because that damage is permanent progress there and wasted on the shield.
  - Follow-up hits stay on the same Gardener when they kill it.
- **Tests 45 and 141** used to expect trading HP. They now expect blocking (6 damage into a 140-HP Eel doesn't remove a turn).
- **New tests:**
  - 183: hallway Defend.
  - 184: the Strike goes to the Gardener nearer death.
  - 185: the boss's exhaust-Block card gets spent.
- **New tool `tools/hp_audit.py`:** measures avoidable damage over the recordings.
  - On the first 2500 recorded turns, hallway loss is 1.87 HP per turn. 1.50 of that can't be avoided with the cards in hand.
  - Most of the rest was chip trades (about 3 HP for 9 damage with no kill), which the model rated as shortening the fight.
  - Hallway damage value is now capped at 0.5 HP per point; elites and bosses keep 0.8. On 700 hallway turns, chip trades fell from 102 HP to 72 HP, and damage dealt fell from 6.6 to 6.4 per turn.
  - Capping at 0.3 would halve chip trades but deal 6% less damage. That's close to even in expected HP, so 0.5 is the middle setting (`VPD_CAP` in `solver.py`).
  - The forced part of hallway damage comes from the deck, which Phases A/B (upgrades, picks) target.
- Replay: 654 turns, mean error 2.3. Tests pass on both builds.
- **Burning Blood fix:** at 79/80, a 4 hit counted as free because the 6 HP heal after the fight would overflow max HP. But that spare room is shared by the whole fight. The free part now subtracts the hits expected on later turns, so with more attacks coming nothing is free. In the Fuzzy Wurm Crawler fight, the advisor now plays Defend + 2 Strikes for 0 HP instead of 3 Strikes for -4 (test 186).
- **Spite fix:** Spite hits twice only if you lost HP this turn, but the solver always counted 2 hits. In the Slithering Strangler fight it planned a kill without the Strength Potion, came up 12 short when Spite hit once, and only then drank the potion for the last two attacks. Spite now counts 1 hit unless the plan loses HP first (Bloodletting, Hemokinesis). HP lost earlier in the turn isn't in the game state, so after such a replan it still counts 1 hit: it under-counts, never over-counts.
- **Strength first:** of plans that end the same way, the one that plays Strength sources (Strength Potion, Inflame...) first is kept, then Vulnerable (Bash), then the attacks. Every later hit gains the bonus, so if the damage falls short the next replan still has it. Strength only adds to card attacks, not to potion damage, orbs or Osty; the solver already did that.
- Tests 187 (Spite one hit, Strength Potion first). Both fail on the old code.
- **Played an extra time:** Duplicator (next card), One-Two Punch (next Attacks this turn), Burst (next Skills this turn) and an active Echo Form (first cards each turn) now replay the card for free. Strength, Vulnerable and Vigor gained so far count on the copy too, so the advisor plays One-Two Punch before the Twin Strike it doubles, after Inflame. Playing Echo Form itself is still valued as "not modeled" (its later turns aren't scored).
- **Dominate:** gains Strength per Vulnerable on its target, counted after its own Vulnerable. Played first, its Vulnerable also boosts the Bash after it.
- **Checked, already right:** Strength adds to each hit of multi-hit cards (Twin Strike, Sword Boomerang, Fight Me!); negative Strength floors at 0 per hit; Fight Me! gives the enemy +1 Strength on its next attack; Strength doesn't touch potion damage, orbs or Osty; Weak 25% / Vulnerable 50% multiply after Strength. Brand goes last on purpose when its exhaust would cost a real card.
- Test 188 (Duplicator, Echo Form, One-Two Punch, Dominate). Replay: 669 turns, mean error 2.3.
- **Map label fix:** with 4 or more paths, the next step showed the game's internal column number ("column 4"), which doesn't match what you see. It now names the step by its place among the paths you can take ("3rd from left"; left / middle / right for 2-3 paths), with a dot row of the paths on screen, ours filled: ○ ○ ● ○.

## v18 (2026-10-21): overlay redesign, game icons
- **New look, toned to the game:** warm dark panel, parchment text, bronze trim and gold section titles. Your character's colour is the energy accent. Fonts are Windows system fonts only (Constantia serif for names and numbers, Segoe UI for small print). The header always reads "Advisor · unofficial", so it never looks like the game's own UI.
- **Layout, top to bottom:**
  - "Autopilot stopped" banner, only when that happens.
  - Header: F8 autopilot pill and the F7 indicator.
  - Screen chip with your stats: HP bar with block, energy, turn, stars/orbs/Osty; gold and deck size outside fights.
  - The advice:
    - Combat: numbered plan, one card-type colour bar per card, target with its intent icon, `?` on unmodeled plays, then DMG / BLOCK / HP / LETHAL chips.
    - Pick screens: ranked rows with icons and score bars. The best is first, and a "dilutes" warning shows in red.
- **F7 = show more / less.** Compact by default: alternatives, pile counts, boss tip, potions, relic count and "not modeled" notes are behind F7. Compact shows a "⚠ N notes" count.
- **Game names:** cards, relics and potions show their English titles from the game's text (`DefendNecrobinder` shows as "Defend").
- **Icons (`advisor/icons.py`) come from your own install:** relics, potions, powers, energy (per character), intents, orbs, map rooms, HP, gold, deck, star.
  - They are read from `SlayTheSpire2.pck` (Godot textures: WebP/PNG, or BC7/DXT GPU formats decoded with Pillow; atlas sprites cropped by their `.tres` region).
  - Coverage on v0.107.1: 296/298 relics, 62/63 potions, all 238 powers, all 33 UI icons. Run `python advisor/icons.py` to check.
  - **Missing icons** (e.g. beta-only relics when the release is installed) are fetched once in the background from `slaythespire.wiki.gg` (`StS2_<Name>.png`).
    - Only that host and https; the file must be a real PNG/WebP under 512 KB.
    - At most one try per day per icon.
  - **Until an icon exists,** a drawn stand-in shows instead: a bronze disc with the first letter. Nothing appears as a blank box.
  - `advisor/icon_cache/` holds the game's art from your copy and the wiki. **Keep it local: never share or upload it.**
- **Demo mode for design checks:** set `STS2_DEMO=advisor/recordings/<day>.jsonl` to replay recorded fights without the game. `STS2_POS=+x+y` places the window, and `STS2_EXPANDED=1` starts expanded.
- Advice logic is unchanged. Tests pass on both builds.

## v17c (2026-10-15): discard / exhaust picks inside a card
- **Cards that make you discard or exhaust from hand are now planned** (Acrobatics, Survivor, Dagger Throw, True Grit, Burning Pact, Brand, Scavenge), read from their ops.
  - The solver picks the card to give up the way you would: Curse/Status first, the most harmful first (Burn's turn-end damage, Regret). For a discard, Sly cards come next, since they get played for free. Then the card worth least next turn.
  - Discarding a Burn now saves its turn-end damage. Exhausting junk is a small gain. Exhausting a good card costs its future value, the same as an Exhaust card.
  - **True Grit (random exhaust):** costs the average loss over the hand.
  - **Exhausts from picks** give Feel No Pain Block and count for Forgotten Ritual (v0.107.1).
  - **A discarded Sly card** is played for free in the plan.
- **Pick screens use the same order:** `solver.toss_key` is shared by the solver and `picker.hand_pick` / `pile_pick`. The plan and the cards F8 clicks agree.
- **Not covered yet** (old path, as before): fetch cards (Headbutt, Hologram) keep the `pile_moves` logic. Cards drawn by the same card (Acrobatics draws 3 first) aren't discard candidates, because they're unknown when planning.
- Tests 179-182 pass on both builds. Replay error unchanged (2.4).

## v17b (2026-10-15): card effects read from the game code
- **What a card does now comes from its own code:** `gen_gamedata.parse_ops` reads each card's `OnPlay()` and stores its effects, in order, as `ops` (and `ops_upgraded` when upgrading changes the effect, like True Grit+).
  - Ops: damage (amount, hits, one/all/random target, Osty), block, power (which power, amount, self/enemy), draw, energy, stars, summon, heal, HP loss, channel, create, pick (select from a pile, then discard/exhaust/upgrade/move), exhaust, upgrade, transform, autoplay, orb ops.
  - Amounts are var names, read from the live card in combat. Upgrades, buffs and beta changes follow without new tables.
  - Coverage: 83.6% of cards fully read on v0.111.0 (82.9% on v0.107.1). The rest are game-computed amounts (Tear Asunder's hits, `Calculated*` vars) or conditions (fatal, "if Poisoned"). `gen_gamedata.py` prints the remaining gaps on each run.
- **The solver plays cards from their ops** (`solver.from_ops`) when it can use every op. Otherwise the card keeps the old var path, unchanged. About 450 cards use the new path.
- **Fixed by reading the code:**
  - Twin Strike, Dagger Spray, Maul, Rip and Tear, Astral Pulse hit twice. The old path counted one hit.
  - Resonance takes Strength from the enemy; Shared Fate costs you Strength and takes it from the enemy.
  - Banshee's Cry, Heavenly Drill, Melancholy, Drum of Battle, Right Hand Hand and Radiate no longer get free Energy/Stars from a cost-reduction or condition var.
  - Uppercut's Weak + Vulnerable come straight from its code. The v16 text patch stays as the fallback.
- **Reader: enchanted cards.** Cards emit `enchantment {name, amount}`, and their vars use the enchanted value (Sharp's damage, Steady's block). **Needs a reader rebuild:** run `run.bat` once (it closes the old reader and rebuilds).
- Tests 173-178 pass on both builds. Replay error unchanged (2.4).

## v17a (2026-10-15): player powers in the turn math (Plating...)
- **Powers come from the game code:** `gen_gamedata.parse_powers` stores each power's type, stack type and hooks (`gamedata["powers"]`). The hooks say when it acts.
  - `.planning/research/powers.md` lists all 246 powers: when each acts, its text, and whether the solver reads it. It also lists the turn-end and HP-loss order taken from the code.
- **Now modeled for you:**
  - **Plating:** Block at your turn end. It isn't changed by Dexterity/Frail and lands after Orichalcum's check. It also counts when played this turn (Stone Armor), plus its future turns (n-1, n-2...).
  - **Buffer:** the next HP loss is 0. **Intangible:** each hit is 1.
  - **Regen** heals at turn end. **Thorns / Flame Barrier** hit back per enemy attack hit.
  - **Block on plays:** Rage (Attacks), Afterimage (every card), Feel No Pain (exhausts). **Juggernaut:** damage per Block gain.
  - **Envenom:** Poison per unblocked hit. **Vigor:** the next Attack hits harder.
  - **Barricade / Blur:** leftover Block counts next turn.
- **HP loss is worked out hit by hit,** in the game's order: your turn-end damage first, then each enemy hit. Block soaks each hit; Osty, Intangible and Buffer apply after it.
- **Lucky Tonic (Buffer) and Heart of Iron (Plating) are now valued.** The solver may drink Lucky Tonic to cancel a big hit when that beats the potion's cost. This is why replay error is 2.4 (2.3 with Buffer off): your recorded fights didn't drink it.
- **`gen_gamedata.py` refresh mode:** `python tools/gen_gamedata.py advisor/gamedata/<build>.json <decomp folder>` rebuilds the code tables and keeps the text, so a build that isn't installed can still be updated.
  - The decompiled code now lives in `sts2-advisor Dev\decomp\sts2src-<version>`.
- Tests 166-172 pass on both builds.

## v16 (2026-10-15): F8 autopilot play fixes
Fixes for problems you saw while F8 was playing:
- **Uppercut before Strike:** the game stores the Weak/Vulnerable of Uppercut, Shockwave, Expose and Putrefy under a generic `Power` var, which the solver never applied. `archetypes.expand_power` names them from the card text; solver, picks and deck sim all use it.
- **Strength before the attack:**
  - Strength from attacks (Setup Strike, Fight Me!, Resonance) now counts, after that card's own hit.
  - Fight Me! gives the enemy +1 Strength; Piercing Wail / Dark Shackles take Strength this turn.
  - Card text sets when a gain lands: Demon Form (later turns), Rupture (on trigger), Friendship (lose Strength, energy each turn), next-turn energy. These used to be added at once.
- **Lasting Strength/Dexterity has value** over the fight's remaining turns (`BUFF_TURNS` 3), so Inflame or Footwork gets played even with no attack this turn.
- **Spare energy:**
  - When plans score the same, the one that spends more energy wins (not once the fight is won).
  - Before pressing E with energy and an affordable card left, the autopilot replans once. It then logs why it ends the turn anyway.
- **Scaling enemies first:** the threat weight looks up to 5 turns ahead (`THREAT_TURNS_MAX`, discount 0.8). A Ritual enemy now draws the hit over a bigger flat hitter.
- **Wrong enemy hit:**
  - The aim clicks only when a second read confirms the hover.
  - On a stale read it goes back to the previous point.
  - Only confirmed spots are remembered, and the spot cache is cleared each fight (combat_ids repeat).
  - The log shows `combat_id` and the aimed point.
  - Labels number same-name enemies among themselves (Louse #1, #2).
- **Forgotten Ritual:** on v0.107.1 it gives energy only after a card was exhausted this turn (in the plan, or earlier this turn from the exhaust pile's growth), and it isn't burned for nothing. On the beta it always gives energy.
- Tests 159-165 pass on both builds. Replay mean error 2.3 (was 2.4).

## 2026-10-15: research sources cleaned
- **Fakes removed from both research sources:** StS1 cards and relics, wrong starter names ("Necro Strike"), StS1 enemies (Gremlin Nob, Lagavulin) and ascension levels A15/A18/A20. StS2 has A1-A10 only.
  - Research Notes: all five guides + Combat Mechanics. Backup in the session scratchpad.
  - Spire-Forge (local only, not pushed): `data/builds.js`, `data/constants.js`, the relic picker in `ui/resultView.js`.
- **Wrong facts fixed against game text:** card costs and effects, relic effects, Poison (decrements by 1), Grand Finale (60 damage).
  - Examples: Reaper Form applies Doom (no lifesteal); Danse Macabre gives Block; Sword Sage gives Replay.
- `import_research.py` re-run: 0 names dropped now (93 guide tiers, 151 pairs, 6 anti lists, 12 boss tips). Tests pass on both builds.
- `builds.md` keeps the fake-name list as a guard. Fixed it: Ink Bottle is real; Consume is not a card.

## 2026-10-15: overlay
- The F8 autopilot hint line is green when the combat autopilot is on, red when off.

## 2026-10-14: multiplayer fixes (beta, live)
- **Reader picks your own player.** It used to read `Players[0]` (the host). It now reads the player whose `NetId` matches `RunManager.RewardSynchronizer._localPlayerId`. HP, deck, hand, piles, potions and relics are now yours.
- **Per-player screens.** Shop stock, rest site options and events are per player slot, and now use your slot.
- **Treasure chest.** New `treasure` field: the relics on offer, from `TreasureRoomRelicSynchronizer._currentRelics`. The overlay shows `Take: <relic>`, ranked by relic value.
- Card rewards already used the screen open on your client, so no change there.

## v15 (2026-10-14): Research Notes + Spire-Forge imports
- **New `tools/import_research.py`** (re-runnable) reads two sources and writes into `knowledge.json`:
  - the Research Notes guides: tier tables -> `cards[X].guide`
  - Spire-Forge, via node `vm`:
    - slaythetierlist tiers -> `cards[X].stl`
    - `SYNERGY_PAIRS` -> `pairs`
    - `ARCHETYPE_ANTI_SYNERGY` -> `anti`
    - `BOSS_TIPS` -> `boss_tips`
  - StS1 leftover names are dropped and listed.
- **Card tier = mean of every source a card has** (Mobalytics, slaythetierlist, guide). The sources disagree on most cards.
- **Two-card combos:** a combo partner already in the deck adds a capped bonus (`picker.combo`).
- **Anti-synergy:** a card that works against a build the deck is committed to costs score.
- **Archetypes:** more core cards and relics per build, from both sources, where the game text fits. New Ironclad **Strikes** build.
- **Relics to avoid:** Snecko Eye and Runic Pyramid are penalised for the builds they hurt.
- **Boss tips** in the overlay during boss fights: the tip for your character, else the general one. The reader now sends `character` in combat.
- **Checked against game code:** "Poison halves" in the notes is wrong; it decrements by 1, as the solver already models.
- Tests 154-158. Details: `.planning/research/builds.md`.

## v14 (2026-10-14): deck dilution in card picks
- **A card must save at least 0.5 HP per act fight** in the deck sim to be worth a slot. The tier list can no longer lift a diluting card over Skip.
- **Unmodeled cards pay the measured slot cost** (`deck.slot_cost`). Before, they paid a flat penalty only past 20 cards.
- **One-turn Strength/Dexterity** from Skills/Attacks counts only when drawn with attacks/block, so co-draw odds matter.
- **Status-adding enemies** (Dazed, Slimed, Burn, Wound...) put dead cards in the sim for those fights. A bigger deck dilutes them. `gen_gamedata` now stores their counts.
- **Overlay:** HP saved per card (`+2.2 HP` / `-1.4 HP: dilutes`), deck size, and dead-card cost.
- Tests 149-153. Details: `.planning/DILUTION.md`.

## v13 (2026-10-14): multi-version support (v0.107.1 + beta v0.111.0)
- **Version-tolerant reads:** `FP/BP` path reads, old path first. The beta moved `CombatManager.IsInProgress` / `_state` into `_turnState`. A path cache covers objects whose type is momentarily unreadable.
- **Optional reader sections fail alone:** a broken section returns null and is named in `warnings`; the rest keeps working.
- **`game_version`** is read from `release_info.json` and shown in the overlay.
- **Per-build game data** in `advisor/gamedata/<version>.json`. The loader picks the installed build; `STS2_VERSION` forces one.
- **Beta data:** v0.111.0 added 18 cards, 2 relics and 1 potion, removed Scare, and changed 62 cards, 17 relics and 12 monsters.
- **Fixes:**
  - The overlay didn't know it was in combat on the beta.
  - The autopilot drank Block Potion after a kill. It now waits for the play pile to resolve.
  - It now replans from live state after every card, so teammate hits and buffs are seen.
  - The pause menu opened after an ignored play. Esc now fires only while a card is held, and an ignored play is retried once.
- Details: `.planning/VERSIONS.md`.

## v12 (2026-10-11): effects audit (energy, draw, piles, hand, deck, HP)
- **Enemy and relic effects modeled:**
  - Vital Spark / Tainted
  - Plow, Burrowed, Ravenous, Imbalanced (stuns and skips)
  - Crab Rage
  - Steam Eruption
  - revive / no-win enemies
  - Thieving Hopper card theft and Fat Gremlin gold theft (kill = loot back)
  - Galvanized, Hexed
  - Pael's Tears / Blood
- **Surrounded / back attacks** (Crusher + Rocket): facing and the 1.5x hit from behind. Enemies on the left side are targeted.
- **Card potions** (choose 1 of 3, random cards) are planned with stand-in cards.
- **Pael event ValueError** fixed (`energyIcons`, nested plurals).
- Details: `.planning/research/effects-audit.md`.

## v11 (2026-10-11): pile awareness
- **The solver reads draw/discard/exhaust.** Next-turn exposure is sampled from the draw pile's contents only (never its hidden order).
- **Exhaust/Retain value from real pile state:** redraws are counted, and Retain is valued with `worth_next`.
- **Draw and fetch cards valued:** Hologram, Headbutt, Dredge, Seeker Strike and fetch potions. In-combat pile picks are auto-clicked and verified by the reader.
- **Deck-grid events say which card to pick:** upgrade, transform, remove, enchant.
- Overlay: pile counts and next-turn unblocked damage.
- Details: `.planning/PILES.md`, `.planning/research/piles.md`.

## v10 (2026-10-11): full-run autopilot groundwork + mechanics + builds
- **Every keyword and character mechanic in the solver:** Exhaust, Ethereal, Retain, Sly, Eternal, orbs, Osty, Doom, Stars, Poison, Shivs, Claw...
- **Enemy-added cards:** Beckon is played, Burn/Infection are blocked.
- **Builds for all 5 characters** (`archetypes.py`): commitment, synergy, relic drawbacks.
- **Deck sim engines:** poison, Doom, Osty, orbs, Stars.
- **Run orchestrator** (`runpilot.py`): loot, map, events, rest, shop, chests. Ascension rules: Poverty, Scarcity, DoubleBoss, A8/A9 numbers.
- **HP-first play:** damage is valued at the deck sim's HP-minimising exchange rate. Random-target hits are expected per enemy.
- F9 full-run autopilot was built, then parked by the user (combat-only F8 stays).
- Details: `.planning/AUTOPILOT.md`.

## v9 (2026-10-11): deck-balance card picks
- **`advisor/deck.py`:** picks, shop and removal are scored by the HP the card saves in the act's boss/elite/hallway fights, using simulated hands. That covers damage vs block balance, draw odds and dilution.
- **Real card costs:** Tangled, Curious, Corruption, Void Form, Free*.
- Turn-end status/curse cards.
- Details: `.planning/DECK-BALANCE.md`.

## v8 (2026-10-11): game-knowledge research
- **Exact enemy AI from the decompiled game:** HP, move cycles, ascension numbers. This gives foresight (`enemies.predict`), threat-weighted targeting and multi-turn danger.
- **Elite/boss/hallway research.** Enemy powers modeled: Skittish, Curl Up, Hardened Shell, Slippery, Intangible, Flutter, Thorns, Ritual...
- **Calibrated fight costs** from your 97 run histories (`tools/calibrate.py`).
- **Mobalytics tiers** for cards, relics and potions.
- **Advice screens:** events and Ancients, Knowledge Demon curse choice, choose-a-card screens.
- **Recorder + `tools/replay.py`** (predicted vs actual HP loss).
- Details: `.planning/RESEARCH.md`, `.planning/research/*`.

## v5-v7: danger, pathing, shop
- **HP in the plan:**
  - low-HP danger weighting
  - tempo value of damage
  - HP-cost cards (Bloodletting) and heal potions
- **Autopilot drinks potions;** a red alert and beep when it stops.
- **Map:** reader map/gold/act/ascension, plus `pathing.py` best routes. Rest advice uses the route ahead.
- **Shop:** reader shop stock, plus the best basket within your gold and potion slots, and the removal target.

## v4: potions + relics
- **Game-data generator** (`tools/gen_gamedata.py`): upgrades, potions, relics.
- **Reader:** potions and relics.
- **24 in-turn relic rules + potions in the solver.** Picks are relic-aware.
- Details: `.planning/PLAN.md`.

## v1-v3: foundation
- **C# ClrMD memory reader** (read-only, no mod).
- **One-turn solver**, tkinter overlay and F8 combat autopilot.
- **Advice:** card picks and rest site.

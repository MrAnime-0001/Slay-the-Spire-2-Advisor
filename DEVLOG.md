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

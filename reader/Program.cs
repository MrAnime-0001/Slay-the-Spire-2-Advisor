// External, read-only state reader for Slay the Spire 2. Field names verified against v0.107.1 and v0.111.0 (beta);
// members a newer build moved are read through FP/BP with the old path first, so every build keeps working.
// Attaches to SlayTheSpire2.exe with ClrMD, walks the managed heap from static singletons,
// and prints one JSON line to stdout whenever the state changes.
using System.Diagnostics;
using System.Text.Json;
using Microsoft.Diagnostics.Runtime;

string[] CardTypes = ["None", "Attack", "Skill", "Power", "Status", "Curse", "Quest"];
string[] TargetTypes = ["None", "Self", "AnyEnemy", "AllEnemies", "RandomEnemy", "AnyPlayer", "AnyAlly", "AllAllies", "TargetedNoCreature", "Osty"];
string[] Phases = ["None", "Start", "AutoPrePlay", "Play", "AutoPostPlay", "End"];
string[] MapTypes = ["Unassigned", "Unknown", "Shop", "Treasure", "RestSite", "Monster", "Elite", "Boss", "Ancient"];
string[] Rarities =["None", "Basic", "Common", "Uncommon", "Rare", "Ancient", "Event", "Token", "Status", "Curse", "Quest"];
string last = "";
var singletons = new Dictionary<string, ulong>();
var instances = new Dictionary<string, (List<ulong> addrs, DateTime scanned)>();
ulong handCombat = 0; // combat the cached NPlayerHand scan belongs to
string[] UiTypes = [.. new[] {
    "Rewards.NRewardButton", "Screens.Map.NNormalMapPoint", "Screens.Map.NBossMapPoint", "Screens.Map.NAncientMapPoint",
    "Events.NEventOptionButton", "RestSite.NRestSiteButton", "Screens.Shops.NMerchantCard", "Screens.Shops.NMerchantRelic",
    "Screens.Shops.NMerchantPotion", "Screens.Shops.NMerchantCardRemoval", "Cards.Holders.NGridCardHolder",
    "Screens.TreasureRoomRelic.NTreasureRoomRelicHolder", "Screens.CharacterSelect.NCharacterSelectButton",
    "Screens.MainMenu.NMainMenuTextButton", "CommonUi.NProceedButton", "CommonUi.NConfirmButton", "CommonUi.NBackButton",
    "Screens.CardSelection.NCardRewardAlternativeButton", "Rooms.NMerchantButton", "TreasureRooms.NTreasureButton",
}.Select(t => "MegaCrit.Sts2.Core.Nodes." + t)];
List<ulong> uiAddrs = [];
DateTime uiScanned = DateTime.MinValue, uiChanged = DateTime.MinValue;
string uiScreen = "";
string gameVersion = "?"; // release_info.json next to the game exe
List<string> warnings = []; // sections that failed this frame (renamed field in another game build)
ulong myCreature = 0; // our Creature: Possess powers keep what they stole per player
int mySlot = 0; // our index in RunState._players: shop inventories, rest sites and events are per player slot
var pathFields = new Dictionary<string, ClrInstanceField>(); // "path#step" -> field, once resolved (see Resolve)

while (true)
{
    var proc = Process.GetProcessesByName("SlayTheSpire2").FirstOrDefault();
    if (proc == null) { Emit(new { status = "waiting_for_game" }); Thread.Sleep(2000); continue; }
    try { Watch(proc); }
    catch (Exception e) { Console.Error.WriteLine($"attach: {e.Message}"); Thread.Sleep(2000); }
}

void Emit(object state)
{
    var json = JsonSerializer.Serialize(state);
    if (json == last) return;
    Console.WriteLine(json);
    last = json;
}

void Watch(Process proc)
{
    gameVersion = GameVersion(proc);
    pathFields.Clear(); // the game (and its build) may have changed
    using var dt = DataTarget.AttachToProcess(proc.Id, suspend: false);
    using var rt = (dt.ClrVersions.FirstOrDefault() ?? throw new Exception("game still loading")).CreateRuntime();
    singletons.Clear();
    instances.Clear();
    while (!proc.HasExited)
    {
        rt.FlushCachedData();
        try { Emit(Snapshot(rt)); }
        catch (Exception e) { Console.Error.WriteLine($"read: {e.Message}"); } // torn read mid-GC: skip this frame
        Thread.Sleep(100);
    }
}

object Snapshot(ClrRuntime rt)
{
    warnings.Clear();
    var runMgr = Singleton(rt, "MegaCrit.Sts2.Core.Runs.RunManager");
    var run = F(runMgr, "State");
    if (run.IsNull) return new { status = "no_run", game_version = gameVersion, ui = Safe("ui", () => Ui(rt, run)), warnings = warnings.ToArray() };
    // Multiplayer: Players[0] is the host. The local player's NetId lives on RunManager's synchronizers
    // (LocalContext.NetId is a static, which ClrMD can't read here).
    var players = Items(F(run, "_players")).ToList();
    var myId = Safe<ulong?>("local_player", () => F(runMgr, "RewardSynchronizer") is { IsNull: false } rs
        ? Field(rs, "_localPlayerId").Read<ulong>(rs.Address, false) : null);
    var player = players.FirstOrDefault(p => myId != null && Field(p, "NetId").Read<ulong>(p.Address, false) == myId);
    if (player.IsNull) player = players.First();
    mySlot = Math.Max(0, players.FindIndex(p => p.Address == player.Address));
    var me = F(player, "Creature");
    myCreature = me.Address;
    if (I(me, "_currentHp") <= 0) // RunState.IsGameOver
        return new { status = "game_over", game_version = gameVersion, player = Creature(me), ui = Safe("ui", () => Ui(rt, run)), warnings = warnings.ToArray() };
    var deck = Items(F(F(player, "Deck"), "_cards")).Select(Card).ToArray();

    var cm = Singleton(rt, "MegaCrit.Sts2.Core.Combat.CombatManager");
    var choose = Safe("choose_card", () => ChooseCard(rt));
    var pcs = F(player, "PlayerCombatState");
    // v0.111.0 moved combat state into CombatManager._turnState (CombatTurnState).
    if (!BP(cm, "IsInProgress", "_turnState.IsInProgress") || pcs.IsNull)
    {
        var outside = new { status = "out_of_combat", game_version = gameVersion, player = Creature(me), deck,
                     card_reward = Safe("card_reward", () => CardReward(runMgr)), rest_site = Safe("rest_site", () => RestSite(runMgr, run)),
                     potions = Potions(player), relics = Relics(player),
                     potion_slots = Items(F(player, "_potionSlots")).Count(), gold = I(player, "_gold"),
                     act = I(run, "_currentActIndex"), ascension = I(run, "AscensionLevel"),
                     seed = Safe("seed", () => Str(F(run, "Rng"), "StringSeed")), // run id: same run across advisor restarts
                     map = Safe("map", () => Map(run)), shop = Safe("shop", () => Shop(run)),
                     act_name = Safe("act_name", () => Items(F(run, "Acts")).ElementAtOrDefault(I(run, "_currentActIndex")) is { IsNull: false } a ? Name(a) : null),
                     map_open = Safe<bool?>("map_open", () => MapOpen(rt)), choose_card = choose,
                     @event = Safe("event", () => Event(runMgr, run)), ui = Safe("ui", () => Ui(rt, run)),
                     card_grid = Safe("card_grid", () => CardGrid(rt)), character = Name(F(player, "Character")),
                     // open chest's relics (multiplayer: one per player, each votes)
                     treasure = Safe("treasure", () => F(runMgr, "TreasureRoomRelicSynchronizer") is { IsNull: false } ts
                         && F(ts, "_currentRelics") is { IsNull: false } cur ? Items(cur).Select(Name).ToArray() : null),
                     warnings = Array.Empty<string>() };
        return outside with { warnings = warnings.ToArray() };
    }
    var cs = FP(cm, "_state", "_turnState.State"); // CombatState

    var fight = new
    {
        status = "combat",
        game_version = gameVersion,
        character = Safe("character", () => Name(F(player, "Character"))), // boss tips per character
        turn = I(pcs, "TurnNumber"),
        phase = Phases.ElementAtOrDefault(I(pcs, "_phase")) ?? "?", // "Play" = player can act
        energy = I(pcs, "_energy"),
        max_energy = I(player, "MaxEnergy"),
        player = Creature(me),
        hand = Items(Pile(pcs, "Hand")).Select(Card).ToArray(),
        // Sorted: the pile's real order is hidden from players (draws take Cards.First()); the advisor only uses
        // what a player knows, the draw pile's contents.
        draw = Items(Pile(pcs, "DrawPile")).OrderBy(Name).Select(Card).ToArray(),
        discard = Items(Pile(pcs, "DiscardPile")).Select(Card).ToArray(),
        exhaust = Items(Pile(pcs, "ExhaustPile")).Select(Card).ToArray(),
        playing = Safe<int?>("playing", () => Items(Pile(pcs, "PlayPile")).Count()), // > 0 while a played card still resolves
        deck,
        potions = Potions(player),
        potion_slots = Items(F(player, "_potionSlots")).Count(),
        relics = Relics(player),
        encounter = Safe("encounter", () => F(cs, "_encounter") is { IsNull: false } enc ? Name(enc) : null), // fight type via gamedata
        enemies = Items(F(cs, "_enemies")).Where(e => I(e, "_currentHp") > 0).Select(Enemy).ToArray(),
        hovered = Safe("hovered", () => Hovered(rt)), // combat_id of the creature under the mouse while targeting (auto-play uses it)
        potion_ui = Safe("potion_ui", () => PotionUi(rt, player)), // which potion slot / Use button is under the mouse (auto-play)
        hand_select = Safe("hand_select", () => HandSelect(rt, cs.Address)), // a card asked you to pick hand cards
        holding = Safe("holding", () => Holding(rt)), // a card is picked up / being aimed (Esc puts it back)
        choose_card = choose, // "choose a card" screen: Knowledge Demon curse, Attack/Skill potions...
        ascension = I(run, "AscensionLevel"), // enemy numbers switch at A8 (HP) / A9 (damage)
        seed = Safe("seed", () => Str(F(run, "Rng"), "StringSeed")), // run id (recordings, run history)
        cards_played = CardsPlayed(cm, cs), // this turn, for Sloth / Ringing / Slow
        // Read from the game's combat history, so an advisor restart mid-turn still knows them.
        my_cards_played = Safe<int?>("my_cards_played", () => ThisRound(cm, cs) // multiplayer: yours only (Velvet Choker, Scarf)
            .Count(e => e.Type!.Name!.EndsWith("CardPlayStartedEntry") && F(e, "Actor").Address == me.Address)),
        exhausted_this_turn = Safe<int?>("exhausted_this_turn", () => ThisRound(cm, cs)
            .Count(e => e.Type!.Name!.EndsWith("CardExhaustedEntry") && F(e, "Actor").Address == me.Address)),
        // Chains of Binding: one Bound card per turn (dupes don't count; Sly auto-plays do, the game checks them too)
        bound_played = Safe<bool?>("bound_played", () => ThisRound(cm, cs).Any(e => e.Type!.Name!.EndsWith("CardPlayStartedEntry")
            && F(e, "Actor").Address == me.Address && F(F(e, "CardPlay"), "Card") is var pc
            && F(pc, "Affliction") is { IsNull: false } aff && Name(aff) == "Bound" && !B(pc, "_isDupe"))),
        skills_played = Safe<int?>("skills_played", () => ThisRound(cm, cs) // Smoggy: one Skill per turn
            .Count(e => e.Type!.Name!.EndsWith("CardPlayStartedEntry") && F(e, "Actor").Address == me.Address
                        && I(F(F(e, "CardPlay"), "Card"), "Type") == 2)),
        attacks_played = Safe<int?>("attacks_played", () => ThisRound(cm, cs) // Juggling: copies the 3rd Attack
            .Count(e => e.Type!.Name!.EndsWith("CardPlayStartedEntry") && F(e, "Actor").Address == me.Address
                        && I(F(F(e, "CardPlay"), "Card"), "Type") == 1)),
        card_grid = Safe("card_grid", () => CardGrid(rt)), // pile pick screen (Headbutt: discard -> top of draw...)
        stars = Safe<int?>("stars", () => I(pcs, "_stars")), // Regent's second resource
        orbs = Safe("orbs", () => Items(F(F(pcs, "OrbQueue"), "_orbs")).Select(o => new { name = Name(o), // Dark grows its evoke, Glass shrinks
            value = (TryField(o, "_evokeVal") ?? TryField(o, "_passiveVal"))?.Read<decimal>(o.Address, false) is { } d ? (int?)(int)d : null }).ToArray()),
        orb_slots = Safe<int?>("orb_slots", () => I(F(pcs, "OrbQueue"), "Capacity")),
        pets = Safe("pets", () => Items(F(pcs, "_pets")).Select(c => new { name = F(c, "Monster") is { IsNull: false } m ? Name(m) : "?", creature = Creature(c) }).ToArray()), // Osty
        warnings = Array.Empty<string>(),
    };
    return fight with { warnings = warnings.ToArray() };
}

T? Safe<T>(string section, Func<T?> read)
{
    // Optional section: a member renamed in another game build disables only this part (reported in `warnings`).
    try { return read(); }
    catch (Exception e) { warnings.Add($"{section}: {e.Message}"); return default; }
}

string GameVersion(Process proc)
{
    try
    {
        var dir = Path.GetDirectoryName(proc.MainModule!.FileName)!;
        using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(dir, "release_info.json")));
        return doc.RootElement.GetProperty("version").GetString() ?? "?";
    }
    catch { return "?"; }
}

bool? Holding(ClrRuntime rt)
{
    // NPlayerHand._currentCardPlay is set while a card is picked up or aimed. Esc with nothing held opens the pause menu.
    foreach (var hand in Instances(rt, "MegaCrit.Sts2.Core.Nodes.Combat.NPlayerHand", force: false).Where(Live))
        if (TryField(hand, "_currentCardPlay") is { } f) return !f.ReadObject(hand.Address, false).IsNull;
    return null;
}

object? HandSelect(ClrRuntime rt, ulong combat)
{
    // NPlayerHand._currentMode SimpleSelect(2)/UpgradeSelect(3): pick cards with number keys, confirm with E.
    // The hand node is rebuilt per combat, so rescan once whenever the combat changes.
    try
    {
        bool fresh = combat != handCombat;
        handCombat = combat;
        foreach (var hand in Instances(rt, "MegaCrit.Sts2.Core.Nodes.Combat.NPlayerHand", force: fresh).Where(Live))
        { // Live: a freed hand from an earlier fight can sit in select mode forever
            int mode = I(hand, "_currentMode");
            if (mode < 2) continue;
            var prefs = Field(hand, "_prefs").ReadStruct(hand.Address, false);
            var prompt = prefs.ReadObjectField("<Prompt>k__BackingField");
            var key = prompt.IsNull ? null : (prompt.Type!.GetFieldByName("<locEntryKey>P")
                                              ?? prompt.Type.GetFieldByName("locEntryKey"))?.ReadString(prompt.Address, false);
            return new { mode = mode == 3 ? "upgrade" : "select", prompt = key,
                         min = prefs.ReadField<int>("<MinSelect>k__BackingField"),
                         max = prefs.ReadField<int>("<MaxSelect>k__BackingField"),
                         selected = F(hand, "_selectedCards").ReadField<int>("_size") };
        }
        return null;
    }
    catch { return null; } // optional: never fail the snapshot over it
}

object[]? CardReward(ClrObject runMgr)
{
    // RewardsSetSynchronizer._rewardStates[].rewardsStack[].set.Rewards[] -> the CardReward whose screen is open
    foreach (var playerState in Items(F(F(runMgr, "RewardsSetSynchronizer"), "_rewardStates")))
    foreach (var setState in Items(F(playerState, "rewardsStack")))
    foreach (var reward in Items(F(F(setState, "set"), "Rewards")))
    {
        var screen = TryField(reward, "_currentlyShownScreen");
        if (screen == null || screen.ReadObject(reward.Address, false).IsNull) continue;
        return Items(F(reward, "_cards")).Select(r =>
        {
            var modified = F(r, "_modifiedCard");
            return Card(modified.IsNull ? F(r, "originalCard") : modified);
        }).ToArray();
    }
    return null;
}

object[] Potions(ClrObject player) =>
    Items(F(player, "_potionSlots")).Select((p, slot) => (p, slot)).Where(x => !x.p.IsNull)
        .Select(x => (object)new { slot = x.slot, name = Name(x.p) }).ToArray();

object[] Relics(ClrObject player) => Items(F(player, "_relics")).Select(r => (object)new
{
    name = Name(r),
    // Each relic's own int/bool fields hold its counters, e.g. Kunai._attacksPlayedThisTurn.
    counters = r.Type!.Fields.Where(f => f.ContainingType?.Name == r.Type.Name)
        .Where(f => f.ElementType is ClrElementType.Int32 or ClrElementType.Boolean)
        .ToDictionary(f => f.Name!, f => f.ElementType == ClrElementType.Boolean
            ? (f.Read<bool>(r.Address, false) ? 1 : 0) : f.Read<int>(r.Address, false)),
}).ToArray();

object? Map(ClrObject run)
{
    // Breadth-first walk of the act map from its start node through each MapPoint's Children set.
    try
    {
        var map = F(run, "Map");
        var start = TryField(map, "StartingMapPoint")?.ReadObject(map.Address, false) ?? default;
        if (start.IsNull) return null;
        int[] At(ClrObject p) { var c = Field(p, "coord").ReadStruct(p.Address, false); return [c.ReadField<int>("col"), c.ReadField<int>("row")]; }
        var nodes = new List<object>();
        var seen = new HashSet<ulong> { start.Address };
        var queue = new Queue<ClrObject>([start]);
        while (queue.Count > 0)
        {
            var p = queue.Dequeue();
            var next = HashItems(F(p, "Children")).ToList();
            foreach (var n in next.Where(n => seen.Add(n.Address))) queue.Enqueue(n);
            var xy = At(p);
            nodes.Add(new { c = xy[0], r = xy[1], type = MapTypes.ElementAtOrDefault(I(p, "PointType")) ?? "?",
                            next = next.Select(At).ToArray() });
        }
        var visited = F(run, "_visitedMapCoords"); // List<MapCoord>: last = current node
        int size = visited.ReadField<int>("_size");
        int[]? current = null;
        if (size > 0)
        {
            var c = visited.ReadObjectField("_items").AsArray().GetStructValue(size - 1);
            current = [c.ReadField<int>("col"), c.ReadField<int>("row")];
        }
        var boss = TryField(map, "BossMapPoint")?.ReadObject(map.Address, false) ?? default;
        return new { current, start = At(start), boss = boss.IsNull ? null : At(boss), nodes };
    }
    catch { return null; } // optional: never fail the snapshot over the map
}

IEnumerable<ClrObject> HashItems(ClrObject set)
{
    // HashSet<T>: _entries[] of {HashCode, Next, Value}, first _count used; Next < -1 marks a free slot.
    if (set.IsNull) yield break;
    var entries = set.ReadObjectField("_entries");
    if (entries.IsNull) yield break;
    var arr = entries.AsArray();
    int n = set.ReadField<int>("_count");
    for (int i = 0; i < n; i++)
    {
        var e = arr.GetStructValue(i);
        if (e.ReadField<int>("Next") >= -1) yield return e.ReadObjectField("Value");
    }
}

object? RestSite(ClrObject runMgr, ClrObject run)
{
    var room = Items(F(run, "_currentRooms")).LastOrDefault();
    if (room.IsNull || Name(room) != "RestSiteRoom") return null;
    var site = Items(F(F(runMgr, "RestSiteSynchronizer"), "_restSites")).ElementAtOrDefault(mySlot);
    if (site.IsNull) return null;
    var chosen = Field(site, "lastChosenOptionIndex").ReadStruct(site.Address, false); // uint?
    return new
    {
        options = Items(F(site, "options")).Select(o => Name(o).Replace("RestSiteOption", "")).ToArray(),
        chosen = chosen.ReadField<bool>("hasValue"),
    };
}

object? Shop(ClrObject run)
{
    // MerchantRoom stock. Bought entries have a null Model (cards: null CreationResult); removal sets Used.
    // ponytail: prices are _cost (sale included) without Hook.ModifyMerchantPrice relic discounts.
    try
    {
        var room = Items(F(run, "_currentRooms")).LastOrDefault();
        if (room.IsNull || Name(room) != "MerchantRoom") return null;
        var inv = Items(F(room, "Inventories")).ElementAtOrDefault(mySlot);
        if (inv.IsNull) return null;
        var cards = Items(F(inv, "_characterCardEntries")).Concat(Items(F(inv, "_colorlessCardEntries")))
            .Where(e => !F(e, "CreationResult").IsNull)
            .Select(e =>
            {
                var r = F(e, "CreationResult");
                var modified = F(r, "_modifiedCard");
                return new { card = Card(modified.IsNull ? F(r, "originalCard") : modified),
                             price = I(e, "_cost"), sale = B(e, "IsOnSale") };
            }).ToArray();
        object[] Stocked(string list) => Items(F(inv, list)).Where(e => !F(e, "Model").IsNull)
            .Select(e => (object)new { name = Name(F(e, "Model")), price = I(e, "_cost") }).ToArray();
        var removal = F(inv, "CardRemovalEntry");
        return new { cards, relics = Stocked("_relicEntries"), potions = Stocked("_potionEntries"),
                     removal = removal.IsNull || B(removal, "Used") ? (int?)null : I(removal, "_cost") };
    }
    catch { return null; } // optional: never fail the snapshot over the shop
}

object? Event(ClrObject runMgr, ClrObject run)
{
    // EventRoom (normal ? events and Ancients): EventSynchronizer._events[mySlot] is our live EventModel.
    // Options carry TextKey + Description LocString (table/key into the game's loc JSON); numbers live in
    // the event's DynamicVars (decimals, or StringValue for names like Relic/Card1).
    try
    {
        var room = Items(F(run, "_currentRooms")).LastOrDefault();
        if (room.IsNull || Name(room) != "EventRoom") return null;
        var ev = Items(F(F(runMgr, "EventSynchronizer"), "_events")).ElementAtOrDefault(mySlot);
        if (ev.IsNull || B(ev, "_isFinished")) return null;
        var vars = new Dictionary<string, object>();
        var set = F(ev, "_dynamicVars");
        if (!set.IsNull)
        {
            var dict = F(set, "_vars");
            var entries = dict.ReadObjectField("_entries");
            int n = dict.ReadField<int>("_count");
            for (int i = 0; !entries.IsNull && i < n; i++)
            {
                var e = entries.AsArray().GetStructValue(i);
                var v = e.ReadObjectField("value");
                if (v.IsNull) continue;
                var key = e.ReadObjectField("key").AsString() ?? "?";
                vars[key] = TryField(v, "StringValue") is { } sv ? sv.ReadString(v.Address, false) ?? ""
                          : (object)(int)Field(v, "_baseValue").Read<decimal>(v.Address, false);
            }
        }
        string? Loc(ClrObject s) => s.IsNull ? null
            : $"{(s.Type!.GetFieldByName("<locTable>P") ?? s.Type.GetFieldByName("locTable"))?.ReadString(s.Address, false)}:"
              + (s.Type.GetFieldByName("<locEntryKey>P") ?? s.Type.GetFieldByName("locEntryKey"))?.ReadString(s.Address, false);
        var options = Items(F(ev, "_currentOptions")).Select(o => new
        {
            key = F(o, "TextKey").AsString(),
            desc = Loc(F(o, "Description")),
            locked = B(o, "IsLocked"),
            proceed = B(o, "IsProceed"),
            relic = F(o, "Relic") is { IsNull: false } r ? Name(r) : null,
        }).ToArray();
        return new { name = Name(ev), ancient = ev.Type!.BaseType?.Name?.EndsWith("AncientEventModel") == true, options, vars };
    }
    catch { return null; }
}

int? CardsPlayed(ClrObject cm, ClrObject cs)
{
    // CombatManager.History._entries: newest last; count CardPlayStartedEntry of the current round.
    try { return ThisRound(cm, cs).Count(e => e.Type!.Name!.EndsWith("CardPlayStartedEntry")); }
    catch { return null; }
}

// CombatManager.History._entries of the current round, newest first.
IEnumerable<ClrObject> ThisRound(ClrObject cm, ClrObject cs)
{
    int round = I(cs, "RoundNumber");
    return Items(F(F(cm, "History"), "_entries")).Reverse().TakeWhile(e => I(e, "RoundNumber") == round);
}

object? ChooseCard(ClrRuntime rt)
{
    // Top of NOverlayStack._overlays is an NChooseACardSelectionScreen: pick one card (click it), maybe skip.
    try
    {
        var top = Items(F(Singleton(rt, "MegaCrit.Sts2.Core.Nodes.Screens.Overlays.NOverlayStack"), "_overlays")).LastOrDefault();
        if (top.IsNull || top.Type?.Name != "MegaCrit.Sts2.Core.Nodes.Screens.CardSelection.NChooseACardSelectionScreen"
            || B(top, "_cardSelected")) return null;
        // hovered = index of the screen's card under the mouse: only holders showing THIS screen's CardModels count,
        // so nodes behind the overlay can't be mistaken for a choice (autopilot clicks on it).
        var models = Items(F(top, "_cards")).Select(c => c.Address).ToList();
        int? hovered = null;
        int matched = 0;
        foreach (var h in Instances(rt, "MegaCrit.Sts2.Core.Nodes.Cards.Holders.NGridCardHolder", force: false).Where(Live))
        {
            var cn = F(h, "CardNode");
            int idx = cn.IsNull ? -1 : models.IndexOf(F(cn, "_model").Address);
            if (idx < 0) continue;
            matched++;
            if (B(h, "_isHovered")) hovered = idx;
        }
        if (matched < models.Count) Instances(rt, "MegaCrit.Sts2.Core.Nodes.Cards.Holders.NGridCardHolder", force: true);
        return new { cards = Items(F(top, "_cards")).Select(Card).ToArray(), can_skip = B(top, "_canSkip"), hovered };
    }
    catch { return null; }
}

object? Ui(ClrRuntime rt, ClrObject run)
{
    // Full autopilot clicks by hover: it sweeps the mouse and clicks once `hover` names the wanted button.
    // Buttons are found by one heap scan over UiTypes (cached; rescanned while a new screen settles, or every
    // 5 s while nothing is hovered). Freed Godot nodes keep their managed wrapper with NativePtr = 0: skipped.
    try
    {
        string screen;
        try
        {
            var top = Items(F(Singleton(rt, "MegaCrit.Sts2.Core.Nodes.Screens.Overlays.NOverlayStack"), "_overlays")).LastOrDefault();
            screen = !top.IsNull ? Name(top) : MapOpen(rt) ? "Map"
                   : run.IsNull ? "Menu" : Items(F(run, "_currentRooms")).LastOrDefault() is { IsNull: false } room ? Name(room) : "?";
        }
        catch { screen = "Menu"; }
        var now = DateTime.Now;
        if (screen != uiScreen) { uiScreen = screen; uiChanged = now; }
        bool settling = now - uiChanged < TimeSpan.FromSeconds(4);
        var nodes = uiAddrs.Select(a => rt.Heap.GetObject(a)).Where(o => o.Type?.Name is { } t && UiTypes.Contains(t)).ToList();
        bool hovered = nodes.Any(o => Live(o) && B(o, "_isHovered"));
        if (now - uiScanned > TimeSpan.FromSeconds(settling ? 1 : 5) && (settling || !hovered || nodes.Count < uiAddrs.Count))
        {
            nodes = rt.Heap.EnumerateObjects().Where(o => o.Type?.Name is { } t && UiTypes.Contains(t)).ToList();
            uiAddrs = nodes.Select(o => o.Address).ToList();
            uiScanned = now;
        }
        string? hover = null;
        var targets = new List<string>();
        foreach (var o in nodes.Where(Live))
        {
            string? d;
            try { d = Describe(o); } catch { d = null; }
            if (d == null) continue;
            if (B(o, "_isHovered")) hover = d;
            if (TryField(o, "_isEnabled") is not { } en || en.Read<bool>(o.Address, false)) targets.Add(d);
        }
        return new { screen, hover, targets = targets.Distinct().ToArray() };
    }
    catch { return null; }
}

bool Live(ClrObject o) => TryField(o, "NativePtr") is not { } p || p.Read<long>(o.Address, false) != 0;

string? Describe(ClrObject o)
{
    // "kind:detail" for the node; null for nodes autopilot never clicks (sold, untravelable, empty).
    string? Model(ClrObject x) => x.IsNull ? null : Name(x);
    string? CardOf(ClrObject node) => F(node, "_model") is { IsNull: false } m ? Name(m) + (I(m, "_currentUpgradeLevel") > 0 ? "+" : "") : null;
    switch (Name(o))
    {
        case "NRewardButton":
            var r = F(o, "Reward");
            return r.IsNull ? null : Name(r) switch
            {
                "GoldReward" => $"reward:gold:{I(r, "Amount")}",
                "PotionReward" => $"reward:potion:{Model(F(r, "Potion"))}",
                "RelicReward" => $"reward:relic:{Model(F(r, "_relic"))}",
                "CardReward" => "reward:card",
                "SpecialCardReward" => $"reward:special:{Model(F(r, "_card"))}",
                "CardRemovalReward" => "reward:remove",
                var n => "reward:" + n,
            };
        case "NNormalMapPoint" or "NBossMapPoint" or "NAncientMapPoint":
            if (I(o, "_state") != 1) return null; // MapPointState.Travelable
            var c = Field(F(o, "Point"), "coord").ReadStruct(F(o, "Point").Address, false);
            return $"map:{c.ReadField<int>("col")},{c.ReadField<int>("row")}";
        case "NEventOptionButton": return "event:" + F(F(o, "Option"), "TextKey").AsString();
        case "NRestSiteButton": return F(o, "_option") is { IsNull: false } opt ? "rest:" + Name(opt).Replace("RestSiteOption", "") : null;
        case "NMerchantCard":
            var cr = F(F(o, "_cardEntry"), "CreationResult");
            if (cr.IsNull) return null;
            var mod = F(cr, "_modifiedCard");
            return "shop:card:" + Name(mod.IsNull ? F(cr, "originalCard") : mod);
        case "NMerchantRelic": return Model(F(F(o, "_relicEntry"), "Model")) is { } rel ? "shop:relic:" + rel : null;
        case "NMerchantPotion": return Model(F(F(o, "_potionEntry"), "Model")) is { } pot ? "shop:potion:" + pot : null;
        case "NMerchantCardRemoval": return B(F(o, "_removalEntry"), "Used") ? null : "shop:remove";
        case "NGridCardHolder": return F(o, "CardNode") is { IsNull: false } cn && CardOf(cn) is { } card ? "card:" + card : null;
        case "NTreasureRoomRelicHolder": return F(o, "Relic") is { IsNull: false } nr && Model(F(nr, "_model")) is { } tr ? "chest:" + tr : null;
        case "NCharacterSelectButton": return B(o, "_isLocked") ? null : "char:" + Model(F(o, "_character"));
        case "NMainMenuTextButton":
            var loc = F(o, "_locString");
            return loc.IsNull ? null : "menu:" + (loc.Type!.GetFieldByName("<locEntryKey>P") ?? loc.Type.GetFieldByName("locEntryKey"))?.ReadString(loc.Address, false);
        case "NCardRewardAlternativeButton": return "alt:" + Str(o, "_optionName");
        default: return Name(o)[1..].Replace("Button", "").ToLower(); // proceed, confirm, back, merchant, treasure
    }
}

object? CardGrid(ClrRuntime rt)
{
    // Deck grids (NDeckUpgrade/CardSelect/Transform/Enchant, NSimpleCardSelectScreen): which cards, what for.
    try
    {
        var top = Items(F(Singleton(rt, "MegaCrit.Sts2.Core.Nodes.Screens.Overlays.NOverlayStack"), "_overlays")).LastOrDefault();
        if (top.IsNull || TryField(top, "_grid") == null) return null;
        string? prompt = null; int min = 1, max = 1;
        if (TryField(top, "_prefs") is { } pf)
        {
            var prefs = pf.ReadStruct(top.Address, false);
            var p = prefs.ReadObjectField("<Prompt>k__BackingField");
            prompt = p.IsNull ? null : (p.Type!.GetFieldByName("<locEntryKey>P") ?? p.Type.GetFieldByName("locEntryKey"))?.ReadString(p.Address, false);
            min = prefs.ReadField<int>("<MinSelect>k__BackingField");
            max = prefs.ReadField<int>("<MaxSelect>k__BackingField");
        }
        // pile (combat pile screens: Headbutt, Hologram...); hovered = index of this screen's card under the mouse,
        // matched through the holders' CardModels like ChooseCard (nothing behind the overlay counts).
        string[] piles = ["None", "Draw", "Hand", "Discard", "Exhaust", "Play", "Deck"];
        var pileObj = TryField(top, "_pile")?.ReadObject(top.Address, false) ?? default;
        var models = Items(F(top, "_cards")).Select(c => c.Address).ToList();
        int? hovered = null;
        int matched = 0;
        foreach (var h in Instances(rt, "MegaCrit.Sts2.Core.Nodes.Cards.Holders.NGridCardHolder", force: false).Where(Live))
        {
            var cn = F(h, "CardNode");
            int idx = cn.IsNull ? -1 : models.IndexOf(F(cn, "_model").Address);
            if (idx < 0) continue;
            matched++;
            if (B(h, "_isHovered")) hovered = idx;
        }
        if (matched < models.Count) Instances(rt, "MegaCrit.Sts2.Core.Nodes.Cards.Holders.NGridCardHolder", force: true);
        return new { screen = Name(top), prompt, min, max, cards = Items(F(top, "_cards")).Select(Card).ToArray(),
                     pile = pileObj.IsNull ? null : piles.ElementAtOrDefault(I(pileObj, "Type")), hovered };
    }
    catch { return null; }
}

bool MapOpen(ClrRuntime rt)
{
    // You stay "in" a shop/event room while the map is up, so the screen state says which advice to show.
    try { return B(Singleton(rt, "MegaCrit.Sts2.Core.Nodes.Screens.Map.NMapScreen"), "IsOpen"); }
    catch { return false; }
}

object? PotionUi(ClrRuntime rt, ClrObject player)
{
    // Potion slots are NPotionHolder UI nodes nothing on the managed side points to, so find them by
    // heap scan (cached). Hover state is NClickableControl._isHovered; an open popup hangs off _popup.
    try
    {
        var slots = Items(F(player, "_potionSlots")).Select(p => p.Address).ToList();
        int? hoveredSlot = null;
        bool popupOpen = false, useHovered = false;
        int matched = 0;
        foreach (var h in Instances(rt, "MegaCrit.Sts2.Core.Nodes.Potions.NPotionHolder", force: false))
        {
            var np = F(h, "Potion");
            int slot = np.IsNull ? -1 : slots.IndexOf(F(np, "_model").Address);
            if (slot < 0) continue;
            matched++;
            if (B(h, "_isHovered")) hoveredSlot = slot;
            var popup = F(h, "_popup");
            if (popup.IsNull || B(popup, "IsMarkedForRemoval")) continue;
            popupOpen = true;
            var use = F(popup, "_useButton");
            useHovered |= !use.IsNull && B(use, "_isHovered");
        }
        if (matched < slots.Count(a => a != 0)) Instances(rt, "MegaCrit.Sts2.Core.Nodes.Potions.NPotionHolder", force: true);
        return new { hovered_slot = hoveredSlot, popup_open = popupOpen, use_hovered = useHovered };
    }
    catch { return null; } // optional, like hover: never fail the snapshot
}

List<ClrObject> Instances(ClrRuntime rt, string type, bool force)
{
    // Cached heap scan (~300 ms). Rescan when an address moved (GC) or when forced, at most every 2 s.
    if (instances.TryGetValue(type, out var c))
    {
        var objs = c.addrs.Select(a => rt.Heap.GetObject(a)).ToList();
        bool valid = objs.All(o => o.Type?.Name == type);
        if ((valid && !force) || DateTime.Now - c.scanned < TimeSpan.FromSeconds(2))
            return objs.Where(o => o.Type?.Name == type).ToList();
    }
    var found = rt.Heap.EnumerateObjects().Where(o => o.Type?.Name == type).ToList();
    instances[type] = (found.Select(o => o.Address).ToList(), DateTime.Now);
    return found;
}

uint? Hovered(ClrRuntime rt)
{
    try
    {
        var node = F(Singleton(rt, "MegaCrit.Sts2.Core.Nodes.Combat.NTargetManager"), "HoveredNode");
        var entity = node.IsNull ? default : TryField(node, "Entity")?.ReadObject(node.Address, false) ?? default;
        return entity.IsNull ? null : CombatId(entity);
    }
    catch { return null; } // hover is optional; never fail the whole snapshot over it
}

uint? CombatId(ClrObject creature)
{
    var id = Field(creature, "CombatId").ReadStruct(creature.Address, false); // uint?
    return id.ReadField<bool>("hasValue") ? id.ReadField<uint>("value") : null;
}

ClrObject Pile(ClrObject pcs, string name) => F(F(pcs, name), "_cards");

object Creature(ClrObject c) => new
{
    hp = I(c, "_currentHp"),
    max_hp = I(c, "_maxHp"),
    block = I(c, "_block"),
    powers = Items(F(c, "_powers")).Select(p => new { name = Name(p), amount = I(p, "_amount"),
                                                       played = TryField(p, "_cardsPlayedThisTurn")?.Read<int>(p.Address, false), // Sloth
                                                       facing = TryField(p, "_facing")?.Read<int>(p.Address, false), // Surrounded: 0 Right, 1 Left
                                                       data = PowerData(p) }).ToArray(),
};

// A power's private per-turn state, so a replan mid-turn sees it: PowerModel._internalData's bool/int/decimal fields
// (Hardened Shell damageReceivedThisTurn, Skittish hasGainedBlockThisTurn...) and Withering Presence's CardsLeft var.
Dictionary<string, object>? PowerData(ClrObject p)
{
    try
    {
        var o = new Dictionary<string, object>();
        var d = TryField(p, "_internalData")?.ReadObject(p.Address, false) ?? default;
        foreach (var f in d.IsNull || d.Type is null ? [] : d.Type.Fields)
        {
            if (f.ElementType == ClrElementType.Boolean) o[f.Name!] = f.Read<bool>(d.Address, false);
            else if (f.ElementType == ClrElementType.Int32) o[f.Name!] = f.Read<int>(d.Address, false);
            else if (f.Type?.Name == "System.Decimal") o[f.Name!] = (double)f.Read<decimal>(d.Address, false);
            else if (f.Type?.Name?.StartsWith("System.Collections.Generic.Dictionary") == true)
            {   // Possess Strength / Speed: stolenStrength / stolenDexterity per player creature -> ours
                var dict = f.ReadObject(d.Address, false);
                var entries = dict.IsNull ? default : dict.ReadObjectField("_entries");
                for (int i = 0, n = dict.IsNull ? 0 : dict.ReadField<int>("_count"); !entries.IsNull && i < n; i++)
                {
                    var e = entries.AsArray().GetStructValue(i);
                    if (e.ReadObjectField("key").Address == myCreature) o[f.Name!] = (double)e.ReadField<decimal>("value");
                }
            }
        }
        if (Name(p) == "WitheringPresencePower")
        {
            var dict = F(F(p, "_dynamicVars"), "_vars");
            var entries = dict.ReadObjectField("_entries");
            for (int i = 0, n = dict.ReadField<int>("_count"); !entries.IsNull && i < n; i++)
            {
                var e = entries.AsArray().GetStructValue(i);
                var v = e.ReadObjectField("value");
                if (!v.IsNull && e.ReadObjectField("key").AsString() == "CardsLeft")
                    o["CardsLeft"] = (int)Field(v, "_baseValue").Read<decimal>(v.Address, false);
            }
        }
        return o.Count > 0 ? o : null;
    }
    catch { return null; }
}

object Enemy(ClrObject c)
{
    var monster = F(c, "Monster");
    var intents = monster.IsNull ? [] : Items(F(F(monster, "NextMove"), "Intents")).Select(Intent).ToArray();
    return new { name = monster.IsNull ? "?" : Name(monster), combat_id = CombatId(c), creature = Creature(c), intents,
                 move = monster.IsNull ? null : Str(F(monster, "NextMove"), "StateId"), // where it is in its AI cycle
                 history = monster.IsNull ? [] : MoveHistory(monster), slot = Str(c, "SlotName") };
}

string[] MoveHistory(ClrObject monster)
{
    // MonsterMoveStateMachine.StateLog: states performed so far (moves and branch points), last 3.
    try { return Items(F(F(monster, "MoveStateMachine"), "StateLog")).Select(s => Str(s, "StateId") ?? "?").TakeLast(3).ToArray(); }
    catch { return []; }
}

string? Str(ClrObject o, string p)
{
    try { return o.IsNull ? null : TryField(o, p)?.ReadString(o.Address, false); }
    catch { return null; }
}

object Intent(ClrObject it)
{
    // Attack intents store damage as `DamageCalc = () => damage`; the int lives in the lambda's closure.
    // 213/215 monster attacks use that form; the rest come back with damage = null.
    int? damage = null;
    var calc = TryField(it, "DamageCalc");
    if (calc != null)
    {
        var closure = calc.ReadObject(it.Address, false).ReadObjectField("_target");
        // Lambdas over a monster property capture the monster itself; Waterfall Giant keeps those in fields.
        var dmg = closure.IsNull ? null : closure.Type!.GetFieldByName("damage")
            ?? closure.Type.GetFieldByName(Name(it) == "DeathBlowIntent" ? "_steamEruptionDamage" : "_currentPressureGunDamage");
        if (dmg != null) damage = dmg.Read<int>(closure.Address, false);
    }
    var repeat = TryField(it, "_repeat");
    // ponytail: hits = 0 means a dynamic repeat count (_repeatCalc) we can't evaluate; solver treats it as 1
    int hits = repeat != null ? repeat.Read<int>(it.Address, false) : calc != null ? 1 : 0;
    if (hits == 0 && TryField(it, "_repeatCalc")?.ReadObject(it.Address, false) is { IsNull: false } rc
        && rc.ReadObjectField("_target") is { IsNull: false } owner && owner.Type!.GetFieldByName("_extraMultiClawCount") is { } extra)
        hits = 3 + extra.Read<int>(owner.Address, false); // Test Subject's Multi Claw: BaseMultiClawCount 3 + extra
    return new { kind = Name(it), damage, hits };
}

int Cost(ClrObject c, ClrObject cost)
{
    // CardEnergyCost.GetWithModifiers(Local): each LocalCostModifier, Absolute (1) sets / Relative (2) adds;
    // IsReduceOnly never raises. Unplayable (<0) and X costs stay as they are.
    if (cost.IsNull) return I(c, "CanonicalEnergyCost");
    int now = I(cost, "_base");
    if (now < 0 || B(cost, "CostsX")) return now;
    foreach (var m in Items(F(cost, "_localModifiers")))
    {
        int a = I(m, "Amount"), type = I(m, "Type");
        bool reduceOnly = B(m, "IsReduceOnly");
        int next = type == 1 ? a : type == 2 ? now + a : now;
        now = reduceOnly ? Math.Min(now, next) : next;
    }
    return Math.Max(0, now);
}

object Card(ClrObject c)
{
    var vars = new Dictionary<string, int>();
    var set = F(c, "_dynamicVars");
    // Enchanted cards (Sharp, Steady...): DynamicVar._enchantedValue = base + the enchantment's Damage/Block change,
    // part of the card in the game's eyes (set when the card is drawn/previewed; reset to base otherwise).
    var ench = F(c, "Enchantment");
    if (!set.IsNull)
    {
        var dict = F(set, "_vars"); // Dictionary<string, DynamicVar>: _entries[] of {key, value}, _count
        var entries = dict.ReadObjectField("_entries");
        int n = dict.ReadField<int>("_count");
        for (int i = 0; !entries.IsNull && i < n; i++)
        {
            var e = entries.AsArray().GetStructValue(i);
            var v = e.ReadObjectField("value");
            if (v.IsNull) continue;
            decimal value = Field(v, "_baseValue").Read<decimal>(v.Address, false);
            if (!ench.IsNull && Field(v, "_enchantedValue").Read<decimal>(v.Address, false) is var ev && ev > 0) value = ev;
            vars[e.ReadObjectField("key").AsString() ?? "?"] = (int)value;
        }
    }
    var cost = F(c, "_energyCost");
    return new
    {
        name = Name(c),
        upgraded = I(c, "_currentUpgradeLevel") > 0,
        type = CardTypes.ElementAtOrDefault(I(c, "Type")) ?? "?",
        target = TargetTypes.ElementAtOrDefault(I(c, "TargetType")) ?? "?",
        rarity = Rarities.ElementAtOrDefault(I(c, "Rarity")) ?? "?",
        // _energyCost is created lazily; until then the printed cost is the ctor's CanonicalEnergyCost.
        cost = Cost(c, cost), // _base + local modifiers ("free this turn"...); power hooks (Tangled...) are in solver.card_cost
        base_cost = cost.IsNull ? I(c, "CanonicalEnergyCost") : I(cost, "_base"),
        x_cost = !cost.IsNull && B(cost, "CostsX"),
        vars,
        affliction = F(c, "Affliction") is { IsNull: false } aff ? Name(aff) : null, // Ringing/Smog/Bound limit plays
        keywords = Keywords(c), // null = the card's printed keywords (gamedata)
        star_cost = B(c, "_starCostSet") ? I(c, "_baseStarCost") : (int?)null, // null = printed (gamedata)
        enchantment = ench.IsNull ? null : new { name = Name(ench), amount = I(ench, "_amount") },
    };
}

string[]? Keywords(ClrObject c)
{
    // CardModel._keywords: HashSet<CardKeyword>, created only once a keyword is added/removed or read.
    string[] names = ["None", "Exhaust", "Ethereal", "Innate", "Unplayable", "Retain", "Sly", "Eternal"];
    var set = F(c, "_keywords");
    if (set.IsNull) return null;
    var entries = set.ReadObjectField("_entries");
    if (entries.IsNull) return [];
    var arr = entries.AsArray();
    var outp = new List<string>();
    for (int i = 0; i < set.ReadField<int>("_count"); i++)
    {
        var e = arr.GetStructValue(i);
        if (e.ReadField<int>("Next") >= -1) outp.Add(names.ElementAtOrDefault(e.ReadField<int>("Value")) ?? "?");
    }
    return [.. outp];
}

// --- ClrMD helpers. Property names resolve to <Prop>k__BackingField, _camelCase, or the exact field name. ---

// ClrMD can't read sts2.dll statics (Godot loads it in its own load context; they read as 0),
// so find each singleton by a one-time heap scan (~300 ms) and recheck the cached address each poll
// (~6 ms). Rescan if the GC moved it.
ClrObject Singleton(ClrRuntime rt, string type)
{
    if (singletons.TryGetValue(type, out var addr))
    {
        var cached = rt.Heap.GetObject(addr);
        if (cached.Type?.Name == type) return cached;
    }
    var found = rt.Heap.EnumerateObjects().FirstOrDefault(o => o.Type?.Name == type);
    if (found.IsNull) throw new Exception($"{type} not on heap yet");
    singletons[type] = found.Address;
    return found;
}

ClrInstanceField? TryField(ClrObject o, string p)
{
    for (var t = o.Type; t != null; t = t.BaseType)
    {
        var f = t.GetFieldByName($"<{p}>k__BackingField") ?? t.GetFieldByName(p)
            ?? t.GetFieldByName("_" + char.ToLower(p[0]) + p[1..]);
        if (f != null) return f;
    }
    return null;
}

ClrInstanceField Field(ClrObject o, string p) =>
    TryField(o, p) ?? throw new Exception($"{o.Type?.Name}.{p} not found (game {gameVersion}: update renamed it?)");

// Version-tolerant reads: each path is dot-separated ("_turnState.IsInProgress") and tried in order (old build
// first). A path whose members exist but passes through a null object reads as null/false.
(bool found, ClrObject obj, ClrInstanceField? field) Resolve(ClrObject o, string path)
{
    var parts = path.Split('.');
    for (int i = 0; i < parts.Length; i++)
    {
        var f = o.IsNull ? null : TryField(o, parts[i]);
        var key = path + "#" + i;
        if (f != null) pathFields[key] = f;
        // A freshly created object (v0.111.0 makes a new CombatTurnState per fight) can briefly have no readable
        // type: reuse the field found before instead of failing every frame (that froze the overlay on "map").
        else if (!o.IsNull && o.Type == null) pathFields.TryGetValue(key, out f);
        if (f == null) return (o.IsNull && i > 0, o, null);
        if (i == parts.Length - 1) return (true, o, f);
        o = f.ReadObject(o.Address, false);
    }
    return (false, o, null);
}

(ClrObject obj, ClrInstanceField? field) Pick(ClrObject o, string[] paths)
{
    foreach (var p in paths)
        if (Resolve(o, p) is { found: true } r) return (r.obj, r.field);
    throw new Exception($"{o.Type?.Name}.{string.Join(" | ", paths)} not found (game {gameVersion}: update renamed it?)");
}

ClrObject FP(ClrObject o, params string[] paths) =>
    Pick(o, paths) is { field: { } f } r ? f.ReadObject(r.obj.Address, false) : default;
bool BP(ClrObject o, params string[] paths) =>
    Pick(o, paths) is { field: { } f } r && f.Read<bool>(r.obj.Address, false);

ClrObject F(ClrObject o, string p) => Field(o, p).ReadObject(o.Address, false);
int I(ClrObject o, string p) => Field(o, p).Read<int>(o.Address, false);
bool B(ClrObject o, string p) => Field(o, p).Read<bool>(o.Address, false);
string Name(ClrObject o) => o.Type!.Name!.Split('.', '+').Last(); // class name: Bash, JawWorm, StrengthPower

IEnumerable<ClrObject> Items(ClrObject o)
{
    // Handles T[], List<T> (_items/_size), and compiler read-only wrappers (_item or _items).
    if (o.IsNull) yield break;
    if (o.IsArray)
    {
        var a = o.AsArray();
        for (int i = 0; i < a.Length; i++) yield return a.GetObjectValue(i);
        yield break;
    }
    if (o.Type!.GetFieldByName("_item") is { } one) { yield return one.ReadObject(o.Address, false); yield break; }
    var items = o.ReadObjectField("_items");
    var arr = items.AsArray();
    int n = o.Type.GetFieldByName("_size") is { } size ? size.Read<int>(o.Address, false) : arr.Length;
    for (int i = 0; i < n; i++) yield return arr.GetObjectValue(i);
}

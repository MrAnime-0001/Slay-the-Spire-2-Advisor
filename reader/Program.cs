// External, read-only state reader for Slay the Spire 2 (field names verified against v0.107.1).
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
    var runMgr = Singleton(rt, "MegaCrit.Sts2.Core.Runs.RunManager");
    var run = F(runMgr, "State");
    if (run.IsNull) return new { status = "no_run" };
    var player = Items(F(run, "_players")).First(); // single-player: local player is Players[0]
    var me = F(player, "Creature");
    if (I(me, "_currentHp") <= 0) return new { status = "game_over", player = Creature(me) }; // RunState.IsGameOver
    var deck = Items(F(F(player, "Deck"), "_cards")).Select(Card).ToArray();

    var cm = Singleton(rt, "MegaCrit.Sts2.Core.Combat.CombatManager");
    var pcs = F(player, "PlayerCombatState");
    if (!B(cm, "IsInProgress") || pcs.IsNull)
        return new { status = "out_of_combat", player = Creature(me), deck, card_reward = CardReward(runMgr),
                     rest_site = RestSite(runMgr, run), potions = Potions(player), relics = Relics(player),
                     potion_slots = Items(F(player, "_potionSlots")).Count(), gold = I(player, "_gold"),
                     act = I(run, "_currentActIndex"), ascension = I(run, "AscensionLevel"), map = Map(run), shop = Shop(run),
                     act_name = Items(F(run, "Acts")).ElementAtOrDefault(I(run, "_currentActIndex")) is { IsNull: false } a ? Name(a) : null,
                     map_open = MapOpen(rt), choose_card = ChooseCard(rt), @event = Event(runMgr, run) };

    return new
    {
        status = "combat",
        turn = I(pcs, "TurnNumber"),
        phase = Phases.ElementAtOrDefault(I(pcs, "_phase")) ?? "?", // "Play" = player can act
        energy = I(pcs, "_energy"),
        max_energy = I(player, "MaxEnergy"),
        player = Creature(me),
        hand = Items(Pile(pcs, "Hand")).Select(Card).ToArray(),
        draw = Items(Pile(pcs, "DrawPile")).Select(Card).ToArray(),
        discard = Items(Pile(pcs, "DiscardPile")).Select(Card).ToArray(),
        exhaust = Items(Pile(pcs, "ExhaustPile")).Select(Card).ToArray(),
        deck,
        potions = Potions(player),
        potion_slots = Items(F(player, "_potionSlots")).Count(),
        relics = Relics(player),
        encounter = F(F(cm, "_state"), "_encounter") is { IsNull: false } enc ? Name(enc) : null, // fight type via gamedata
        enemies = Items(F(F(cm, "_state"), "_enemies")).Where(e => I(e, "_currentHp") > 0).Select(Enemy).ToArray(),
        hovered = Hovered(rt), // combat_id of the creature under the mouse while targeting (auto-play uses it)
        potion_ui = PotionUi(rt, player), // which potion slot / Use button is under the mouse (auto-play)
        hand_select = HandSelect(rt, F(cm, "_state").Address), // a card asked you to pick hand cards
        choose_card = ChooseCard(rt), // "choose a card" screen: Knowledge Demon curse, Attack/Skill potions...
        ascension = I(run, "AscensionLevel"), // enemy numbers switch at A8 (HP) / A9 (damage)
        cards_played = CardsPlayed(cm), // this turn, for Sloth / Ringing / Slow
    };
}

object? HandSelect(ClrRuntime rt, ulong combat)
{
    // NPlayerHand._currentMode SimpleSelect(2)/UpgradeSelect(3): pick cards with number keys, confirm with E.
    // The hand node is rebuilt per combat, so rescan once whenever the combat changes.
    try
    {
        bool fresh = combat != handCombat;
        handCombat = combat;
        foreach (var hand in Instances(rt, "MegaCrit.Sts2.Core.Nodes.Combat.NPlayerHand", force: fresh))
        {
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
    var site = Items(F(F(runMgr, "RestSiteSynchronizer"), "_restSites")).FirstOrDefault(); // single-player
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
        var inv = Items(F(room, "Inventories")).FirstOrDefault(); // single-player
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
    // EventRoom (normal ? events and Ancients): EventSynchronizer._events[0] is our live EventModel.
    // Options carry TextKey + Description LocString (table/key into the game's loc JSON); numbers live in
    // the event's DynamicVars (decimals, or StringValue for names like Relic/Card1).
    try
    {
        var room = Items(F(run, "_currentRooms")).LastOrDefault();
        if (room.IsNull || Name(room) != "EventRoom") return null;
        var ev = Items(F(F(runMgr, "EventSynchronizer"), "_events")).FirstOrDefault();
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

int? CardsPlayed(ClrObject cm)
{
    // CombatManager.History._entries: newest last; count CardPlayStartedEntry of the current round.
    try
    {
        int round = I(F(cm, "_state"), "RoundNumber");
        return Items(F(F(cm, "History"), "_entries")).Reverse().TakeWhile(e => I(e, "RoundNumber") == round)
            .Count(e => e.Type!.Name!.EndsWith("CardPlayStartedEntry"));
    }
    catch { return null; }
}

object? ChooseCard(ClrRuntime rt)
{
    // Top of NOverlayStack._overlays is an NChooseACardSelectionScreen: pick one card (click it), maybe skip.
    try
    {
        var top = Items(F(Singleton(rt, "MegaCrit.Sts2.Core.Nodes.Screens.Overlays.NOverlayStack"), "_overlays")).LastOrDefault();
        if (top.IsNull || top.Type?.Name != "MegaCrit.Sts2.Core.Nodes.Screens.CardSelection.NChooseACardSelectionScreen"
            || B(top, "_cardSelected")) return null;
        return new { cards = Items(F(top, "_cards")).Select(Card).ToArray(), can_skip = B(top, "_canSkip") };
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
                                                       played = TryField(p, "_cardsPlayedThisTurn")?.Read<int>(p.Address, false) }).ToArray(), // Sloth
};

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
        var dmg = closure.IsNull ? null : closure.Type!.GetFieldByName("damage");
        if (dmg != null) damage = dmg.Read<int>(closure.Address, false);
    }
    var repeat = TryField(it, "_repeat");
    // ponytail: hits = 0 means a dynamic repeat count (_repeatCalc) we can't evaluate; solver treats it as 1
    int hits = repeat != null ? repeat.Read<int>(it.Address, false) : calc != null ? 1 : 0;
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
    if (!set.IsNull)
    {
        var dict = F(set, "_vars"); // Dictionary<string, DynamicVar>: _entries[] of {key, value}, _count
        var entries = dict.ReadObjectField("_entries");
        int n = dict.ReadField<int>("_count");
        for (int i = 0; !entries.IsNull && i < n; i++)
        {
            var e = entries.AsArray().GetStructValue(i);
            var v = e.ReadObjectField("value");
            if (!v.IsNull) vars[e.ReadObjectField("key").AsString() ?? "?"] = (int)Field(v, "_baseValue").Read<decimal>(v.Address, false);
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
    };
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
    TryField(o, p) ?? throw new Exception($"{o.Type?.Name}.{p} not found (game update renamed it?)");

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

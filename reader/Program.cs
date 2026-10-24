// External, read-only state reader for Slay the Spire 2 (field names verified against v0.107.1).
// Attaches to SlayTheSpire2.exe with ClrMD, walks the managed heap from static singletons,
// and prints one JSON line to stdout whenever the state changes.
using System.Diagnostics;
using System.Text.Json;
using Microsoft.Diagnostics.Runtime;

string[] CardTypes = ["None", "Attack", "Skill", "Power", "Status", "Curse", "Quest"];
string[] TargetTypes = ["None", "Self", "AnyEnemy", "AllEnemies", "RandomEnemy", "AnyPlayer", "AnyAlly", "AllAllies", "TargetedNoCreature", "Osty"];
string last = "";
var singletons = new Dictionary<string, ulong>();

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
    while (!proc.HasExited)
    {
        rt.FlushCachedData();
        try { Emit(Snapshot(rt)); }
        catch (Exception e) { Console.Error.WriteLine($"read: {e.Message}"); } // torn read mid-GC: skip this frame
        Thread.Sleep(250);
    }
}

object Snapshot(ClrRuntime rt)
{
    var run = F(Singleton(rt, "MegaCrit.Sts2.Core.Runs.RunManager"), "State");
    if (run.IsNull) return new { status = "no_run" };
    var player = Items(F(run, "_players")).First(); // single-player: local player is Players[0]
    var me = F(player, "Creature");
    var deck = Items(F(F(player, "Deck"), "_cards")).Select(Card).ToArray();

    var cm = Singleton(rt, "MegaCrit.Sts2.Core.Combat.CombatManager");
    var pcs = F(player, "PlayerCombatState");
    if (!B(cm, "IsInProgress") || pcs.IsNull)
        return new { status = "out_of_combat", player = Creature(me), deck };

    return new
    {
        status = "combat",
        turn = I(pcs, "TurnNumber"),
        energy = I(pcs, "_energy"),
        max_energy = I(player, "MaxEnergy"),
        player = Creature(me),
        hand = Items(Pile(pcs, "Hand")).Select(Card).ToArray(),
        draw = Items(Pile(pcs, "DrawPile")).Select(Card).ToArray(),
        discard = Items(Pile(pcs, "DiscardPile")).Select(Card).ToArray(),
        exhaust = Items(Pile(pcs, "ExhaustPile")).Select(Card).ToArray(),
        deck,
        enemies = Items(F(F(cm, "_state"), "_enemies")).Where(e => I(e, "_currentHp") > 0).Select(Enemy).ToArray(),
    };
}

ClrObject Pile(ClrObject pcs, string name) => F(F(pcs, name), "_cards");

object Creature(ClrObject c) => new
{
    hp = I(c, "_currentHp"),
    max_hp = I(c, "_maxHp"),
    block = I(c, "_block"),
    powers = Items(F(c, "_powers")).Select(p => new { name = Name(p), amount = I(p, "_amount") }).ToArray(),
};

object Enemy(ClrObject c)
{
    var monster = F(c, "Monster");
    var intents = monster.IsNull ? [] : Items(F(F(monster, "NextMove"), "Intents")).Select(Intent).ToArray();
    return new { name = monster.IsNull ? "?" : Name(monster), creature = Creature(c), intents };
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
        cost = cost.IsNull ? 0 : I(cost, "_base"), // ponytail: ignores temporary cost modifiers
        x_cost = !cost.IsNull && B(cost, "CostsX"),
        vars,
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

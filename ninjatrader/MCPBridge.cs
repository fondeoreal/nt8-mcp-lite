#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

// =====================================================================
//  MCPBridge.cs  v3  -  Puente TCP/JSON de SOLO LECTURA entre NinjaTrader 8
//  y el servidor MCP (y a traves de el, cualquier IA).
//
//  Coloca este fichero en:
//    Documents\NinjaTrader 8\bin\Custom\AddOns\MCPBridge.cs
//  y compila con F5 desde el NinjaScript Editor.
//
//  POLITICA DE ORDENES (fija en el codigo, no configurable desde fuera):
//    Este puente NO puede enviar, modificar ni cancelar ordenes, ni aplanar
//    posiciones, ni arrancar/parar estrategias. El codigo que lo hacia se ha
//    eliminado. Los comandos antiguos responden con un error "BLOQUEADO".
//    El puente de reflexion solo puede LEER cualquier objeto; invocar metodos
//    o asignar propiedades esta limitado a objetos creados por el propio
//    puente y a los tipos del Strategy Analyzer (ver ReflectionPolicy).
//
//  Conexion: token y puerto se leen de Documents\NinjaTrader 8\mcp_bridge.json
//    {"token":"...","port":36980}
//  (el mismo fichero lo lee el servidor MCP en Python). Si no existe, se usan
//  las constantes DefaultToken / DefaultPort.
//
//  Protocolo: una peticion JSON por linea (\n), una respuesta JSON por linea.
//    -> {"id":"1","token":"...","cmd":"positions","args":{"account":"Sim101"}}
//    <- {"id":"1","ok":true,"data":{...}}
//    <- {"id":"1","ok":false,"error":"mensaje"}
//
//  Sin dependencias externas: parser/serializador JSON minimo incluido.
//  OJO: el puerto 36973 es el del ATI de NinjaTrader; no lo uses aqui.
// =====================================================================

namespace NinjaTrader.NinjaScript.AddOns
{
    public partial class MCPBridge : AddOnBase
    {
        // 3.1.0: escritura SOLO en cuentas del simulador (MCPSim.cs); las cuentas reales siguen en solo lectura.
        public const string BridgeVersion = "3.1.0";

        // ---------------- Configuracion ----------------
        private const string DefaultToken  = "CAMBIA-ESTE-TOKEN";
        private const int    DefaultPort   = 36980;
        private const string BindAddress   = "127.0.0.1";   // solo loopback: no exponer a la red
        private const string SettingsFile  = "mcp_bridge.json";
        private const int    MaxLogLines   = 500;
        private const int    MaxEvents     = 3000;
        private const int    MaxLineChars  = 4 * 1024 * 1024;
        private const int    MaxBarsInline = 20000;

        // Prefijos de tipo extra sobre los que la reflexion puede invocar/asignar.
        // Anade aqui tipos propios si de verdad lo necesitas (y recompila).
        private static readonly string[] ExtraWritableTypePrefixes = new string[0];

        private string token = DefaultToken;
        private int    port  = DefaultPort;

        // ---------------- Estado interno ----------------
        private TcpListener listener;
        private Thread      acceptThread;
        private volatile bool running;

        private readonly object gate = new object();
        private readonly Dictionary<string, MarketTick> tickCache  = new Dictionary<string, MarketTick>();
        private readonly HashSet<string>                subscribed = new HashSet<string>();
        private readonly List<string>                   eventLog   = new List<string>();

        // Feed de eventos (solo lectura): ordenes, ejecuciones, posiciones, conexiones.
        private readonly LinkedList<Dictionary<string, object>> events = new LinkedList<Dictionary<string, object>>();
        private long eventSeq = 0;
        private readonly HashSet<Account> hookedAccounts = new HashSet<Account>();

        // Reflexion: objetos vivos referenciados por handle ("h1", "h2"...).
        private readonly Dictionary<string, object> handles = new Dictionary<string, object>();
        private int handleSeq = 0;
        // Objetos creados por el propio puente (reflect_new / sa_run): los unicos
        // no pertenecientes al Strategy Analyzer sobre los que se puede escribir.
        private readonly HashSet<object> owned = new HashSet<object>(new RefEq());

        // Jobs asincronos (backtests y exportaciones de historico).
        private readonly Dictionary<string, Dictionary<string, object>> jobs = new Dictionary<string, Dictionary<string, object>>();
        private int jobSeq = 0;
        private volatile bool saBusy;

        private class MarketTick
        {
            public double Last, Bid, Ask;
            public long   Volume;
            public DateTime Time;
        }

        private class RefEq : IEqualityComparer<object>
        {
            public new bool Equals(object x, object y) { return ReferenceEquals(x, y); }
            public int GetHashCode(object o) { return System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(o); }
        }

        // ---------------- Ciclo de vida ----------------
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "MCPBridge";
                Description = "Puente TCP/JSON de solo lectura para exponer NT8 a un servidor MCP.";
            }
            else if (State == State.Configure)
            {
                StartServer();
            }
            else if (State == State.Terminated)
            {
                StopServer();
            }
        }

        private void LoadSettings()
        {
            try
            {
                string path = Path.Combine(Globals.UserDataDir, SettingsFile);
                if (!File.Exists(path)) return;
                var d = Js.Parse(File.ReadAllText(path)) as Dictionary<string, object>;
                if (d == null) return;
                string t = Str(d, "token", null);
                if (!string.IsNullOrEmpty(t) && t.Length >= 16) token = t;
                int p = (int)Num(d, "port", DefaultPort);
                if (p > 1024 && p < 65536 && p != 36973) port = p;   // 36973 = ATI de NinjaTrader
            }
            catch (Exception ex) { Log("mcp_bridge.json ilegible: " + ex.Message); }
        }

        private void StartServer()
        {
            if (running) return;
            try
            {
                LoadSettings();
                listener = new TcpListener(IPAddress.Parse(BindAddress), port);
                listener.Start();
                running = true;

                acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "MCPBridgeAccept" };
                acceptThread.Start();

                HookAccounts();
                try { Connection.ConnectionStatusUpdate += OnConnectionStatus; } catch { }

                Log("MCPBridge v" + BridgeVersion + " (solo lectura de ordenes) escuchando en " + BindAddress + ":" + port);
            }
            catch (Exception ex)
            {
                Log("ERROR al arrancar MCPBridge: " + ex.Message);
            }
        }

        private void StopServer()
        {
            running = false;
            try
            {
                lock (gate)
                {
                    foreach (Account a in hookedAccounts)
                    {
                        try
                        {
                            a.ExecutionUpdate -= OnExecutionUpdate;
                            a.OrderUpdate     -= OnOrderUpdate;
                            a.PositionUpdate  -= OnPositionUpdate;
                        }
                        catch { }
                    }
                    hookedAccounts.Clear();
                }
            }
            catch { }
            try { Connection.ConnectionStatusUpdate -= OnConnectionStatus; } catch { }

            lock (gate)
            {
                foreach (string key in subscribed.ToList())
                {
                    try
                    {
                        Instrument ins = ResolveInstrument(key);
                        if (ins != null && ins.MarketData != null)
                            ins.MarketData.Update -= OnMarketData;
                    }
                    catch { }
                }
                subscribed.Clear();
                handles.Clear();
                owned.Clear();
            }

            try { if (listener != null) listener.Stop(); } catch { }
            listener = null;
        }

        private void HookAccounts()
        {
            try
            {
                foreach (Account a in Account.All.ToList())
                {
                    lock (gate) { if (!hookedAccounts.Add(a)) continue; }
                    try
                    {
                        a.ExecutionUpdate += OnExecutionUpdate;
                        a.OrderUpdate     += OnOrderUpdate;
                        a.PositionUpdate  += OnPositionUpdate;
                    }
                    catch { }
                }
            }
            catch { }
        }

        private void AcceptLoop()
        {
            while (running)
            {
                try
                {
                    TcpClient client = listener.AcceptTcpClient();
                    Thread t = new Thread(() => HandleClient(client)) { IsBackground = true, Name = "MCPBridgeClient" };
                    t.Start();
                }
                catch (SocketException) { break; }
                catch (ObjectDisposedException) { break; }
                catch (Exception ex) { Log("Accept: " + ex.Message); Thread.Sleep(200); }
            }
        }

        private void HandleClient(TcpClient client)
        {
            // Este metodo es el punto de entrada de un hilo de fondo: si deja escapar una
            // excepcion, .NET tumba todo NinjaTrader. Todo va envuelto y los fallos de red
            // solo cierran esta conexion.
            try
            {
                using (client)
                using (NetworkStream ns = client.GetStream())
                using (StreamReader reader = new StreamReader(ns, new UTF8Encoding(false)))
                using (StreamWriter writer = new StreamWriter(ns, new UTF8Encoding(false)) { AutoFlush = true })
                {
                    client.NoDelay = true;
                    string line;
                    while (running && (line = reader.ReadLine()) != null)
                    {
                        if (line.Trim().Length == 0) continue;
                        string response;
                        string id = "";
                        try
                        {
                            if (line.Length > MaxLineChars) throw new Exception("peticion demasiado grande");
                            Dictionary<string, object> req = Js.Parse(line) as Dictionary<string, object>;
                            if (req == null) throw new Exception("peticion JSON invalida");

                            id = Str(req, "id", "");
                            // El token por defecto es publico (igual en todas las instalaciones): no se atiende con el.
                            if (token == "CAMBIA-ESTE-TOKEN")
                                throw new Exception("el puente usa el token por defecto: ejecuta Instalar.cmd (o crea "
                                    + "Documents\\NinjaTrader 8\\mcp_bridge.json con un token propio de 16+ caracteres) y reinicia NinjaTrader");
                            if (!TokenEquals(Str(req, "token", ""), token)) throw new Exception("token invalido");

                            string cmd = Str(req, "cmd", "");
                            Dictionary<string, object> args = req.ContainsKey("args")
                                ? req["args"] as Dictionary<string, object>
                                : new Dictionary<string, object>();
                            if (args == null) args = new Dictionary<string, object>();

                            object data = Dispatch(cmd, args);
                            var ok = new Dictionary<string, object> { { "id", id }, { "ok", true }, { "data", data } };
                            response = Js.Write(ok);
                        }
                        catch (Exception ex)
                        {
                            var err = new Dictionary<string, object> { { "id", id }, { "ok", false }, { "error", Unwrap(ex).Message } };
                            response = Js.Write(err);
                        }
                        try { writer.WriteLine(response); }
                        catch (IOException) { break; }
                        catch (ObjectDisposedException) { break; }
                    }
                }
            }
            catch (IOException) { }
            catch (ObjectDisposedException) { }
            catch (SocketException) { }
            catch (Exception ex) { try { Log("HandleClient: " + ex.Message); } catch { } }
        }

        private static bool TokenEquals(string a, string b)
        {
            if (a == null || b == null || a.Length != b.Length) return false;
            int diff = 0;
            for (int i = 0; i < a.Length; i++) diff |= a[i] ^ b[i];
            return diff == 0;
        }

        // ---------------- Router de comandos ----------------
        private static readonly HashSet<string> BlockedCommands = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "place_order", "place_bracket", "submit_order", "change_order", "modify_order",
            "cancel_order", "cancel_all", "flatten", "close_position", "reverse_position",
            "strategy_stop", "strategy_start", "strategy_enable", "strategy_disable",
            "atm_start", "atm_close", "set_kill_switch"
        };

        private object Dispatch(string cmd, Dictionary<string, object> a)
        {
            if (BlockedCommands.Contains(cmd))
                throw new Exception("BLOQUEADO: '" + cmd + "' no esta disponible. Este puente es de SOLO LECTURA: "
                    + "no puede enviar, modificar ni cancelar ordenes, aplanar posiciones ni arrancar/parar estrategias.");
            // Escritura solo en cuentas del simulador (MCPSim.cs): cada comando comprueba Provider.Simulator.
            if (SimCommands.Contains(cmd)) return DispatchSim(cmd, a);

            switch (cmd)
            {
                case "ping":              return CmdPing();
                case "capabilities":      return CmdCapabilities();
                case "enums":             return CmdEnums();
                case "accounts":          return CmdAccounts();
                case "accounts_overview": return CmdAccountsOverview();
                case "account_status":    return CmdAccountStatus(a);
                case "account_items":     return CmdAccountItems(a);
                case "positions":         return CmdPositions(a);
                case "orders":            return CmdOrders(a);
                case "executions":        return CmdExecutions(a);
                case "connections":       return CmdConnections();
                case "instrument_info":   return CmdInstrumentInfo(a);
                case "instrument_search": return CmdInstrumentSearch(a);
                case "trading_hours":     return CmdTradingHours(a);
                case "quote":             return CmdQuote(a);
                case "quotes":            return CmdQuotes(a);
                case "bars":              return CmdBars(a);
                case "bars_export":       return CmdBarsExport(a);
                case "events":            return CmdEvents(a);
                case "strategies":        return CmdStrategies(a);
                case "strategy_types":    return CmdStrategyTypes(a);
                case "strategy_defaults": return CmdStrategyDefaults(a);
                case "strategy_params":   return CmdStrategyParams(a);
                case "log":               return CmdLog(a);
                case "job_status":        return CmdJobStatus(a);
                case "jobs":              return CmdJobs();
                // ---- Reflexion (lectura libre, escritura restringida) ----
                case "reflect_find":      return CmdReflectFind(a);
                case "reflect_members":   return CmdReflectMembers(a);
                case "reflect_new":       return CmdReflectNew(a);
                case "reflect_get":       return CmdReflectGet(a);
                case "reflect_set":       return CmdReflectSet(a);
                case "reflect_call":      return CmdReflectCall(a);
                case "reflect_handles":   return CmdReflectHandles(a);
                case "reflect_release":   return CmdReflectRelease(a);
                case "reflect_policy":    return CmdReflectPolicy();
                // ---- Strategy Analyzer ----
                case "sa_windows":        return CmdSaWindows();
                case "sa_open":           return CmdSaOpen(a);
                case "sa_run":            return CmdSaRun(a);
                case "sa_status":         return CmdJobStatus(a);
                case "sa_jobs":           return CmdJobs();
                default: throw new Exception("comando desconocido: " + cmd);
            }
        }

        private object CmdPing()
        {
            return new Dictionary<string, object>
            {
                { "pong", true },
                { "bridge_version", BridgeVersion },
                { "read_only_orders", true },
                { "sim_write", false },
                { "sim_version", SimVersion },
                { "install_dir", Globals.InstallDir },
                { "user_data_dir", Globals.UserDataDir },
                { "port", port },
                { "server_time", DateTime.Now.ToString("o", CultureInfo.InvariantCulture) },
                { "utc_offset_minutes", (int)TimeZoneInfo.Local.GetUtcOffset(DateTime.Now).TotalMinutes },
                { "connections", Connection.Connections.Count },
                { "accounts", Account.All.Count }
            };
        }

        private object CmdCapabilities()
        {
            return new Dictionary<string, object>
            {
                { "bridge_version", BridgeVersion },
                { "read_only_orders", true },
                { "sim_write", false },
                { "sim_commands", SimCommands.OrderBy(x => x).Select(x => (object)x).ToList() },
                { "blocked_commands", BlockedCommands.OrderBy(x => x).Select(x => (object)x).ToList() },
                { "commands", new List<object> {
                    "ping","capabilities","enums","accounts","accounts_overview","account_status","account_items",
                    "positions","orders","executions","connections","instrument_info","instrument_search",
                    "trading_hours","quote","quotes","bars","bars_export","events","strategies","strategy_types",
                    "strategy_defaults","strategy_params","log","job_status","jobs","reflect_find","reflect_members",
                    "reflect_new","reflect_get","reflect_set","reflect_call","reflect_handles","reflect_release",
                    "reflect_policy","sa_windows","sa_open","sa_run","sa_status","sa_jobs" } }
            };
        }

        // Mapas numero -> nombre de los enums, para decodificar la base de datos SQLite de NT8.
        private object CmdEnums()
        {
            var types = new Type[]
            {
                typeof(AccountItem), typeof(OrderState), typeof(OrderAction), typeof(OrderType),
                typeof(TimeInForce), typeof(MarketPosition), typeof(Provider), typeof(Currency),
                typeof(Exchange), typeof(InstrumentType), typeof(OrderEntry), typeof(ConnectionStatus),
                typeof(ErrorCode), typeof(BarsPeriodType), typeof(MarketDataType)
            };
            var result = new Dictionary<string, object>();
            foreach (Type t in types)
            {
                var map = new Dictionary<string, object>();
                foreach (object v in Enum.GetValues(t))
                {
                    string key = Convert.ToInt64(v, CultureInfo.InvariantCulture).ToString(CultureInfo.InvariantCulture);
                    if (!map.ContainsKey(key)) map[key] = v.ToString();
                }
                result[t.Name] = map;
            }
            return result;
        }

        // ---------------- Cuentas ----------------
        private object CmdAccounts()
        {
            var list = new List<object>();
            foreach (Account acc in Account.All)
            {
                list.Add(new Dictionary<string, object>
                {
                    { "name", acc.Name },
                    { "display_name", GetMemberSafe(acc, "DisplayName") as string },
                    { "provider", acc.Provider.ToString() },
                    { "connection", acc.Connection != null ? acc.Connection.Options.Name : null },
                    { "connected", acc.Connection != null && acc.Connection.Status == ConnectionStatus.Connected },
                    { "denomination", acc.Denomination.ToString() },
                    { "simulation", acc.Provider == Provider.Simulator || acc.Provider == Provider.Playback },
                    { "status", ToText(GetMemberSafe(acc, "AccountStatus")) }
                });
            }
            return list;
        }

        private object CmdAccountsOverview()
        {
            var list = new List<object>();
            foreach (Account acc in Account.All)
            {
                double unreal = 0;
                foreach (Position p in PositionsOf(acc)) unreal += UnrealizedPnL(p);
                list.Add(new Dictionary<string, object>
                {
                    { "account", acc.Name },
                    { "connection", acc.Connection != null ? acc.Connection.Options.Name : null },
                    { "connected", acc.Connection != null && acc.Connection.Status == ConnectionStatus.Connected },
                    { "cash_value", SafeGet(acc, AccountItem.CashValue) },
                    { "net_liquidation", SafeGet(acc, AccountItem.NetLiquidation) },
                    { "realized_pnl", SafeGet(acc, AccountItem.RealizedProfitLoss) },
                    { "unrealized_pnl", unreal },
                    { "open_positions", PositionsOf(acc).Length },
                    { "working_orders", OrdersOf(acc).Count(IsActive) }
                });
            }
            return list;
        }

        private object CmdAccountStatus(Dictionary<string, object> a)
        {
            Account acc = ResolveAccount(Str(a, "account", null));
            double unreal = 0;
            foreach (Position p in PositionsOf(acc)) unreal += UnrealizedPnL(p);
            double realiz = SafeGet(acc, AccountItem.RealizedProfitLoss);

            return new Dictionary<string, object>
            {
                { "account", acc.Name },
                { "provider", acc.Provider.ToString() },
                { "connection", acc.Connection != null ? acc.Connection.Options.Name : null },
                { "cash_value", SafeGet(acc, AccountItem.CashValue) },
                { "net_liquidation", SafeGet(acc, AccountItem.NetLiquidation) },
                { "realized_pnl", realiz },
                { "unrealized_pnl", unreal },
                { "total_pnl", realiz + unreal },
                { "buying_power", SafeGet(acc, AccountItem.BuyingPower) },
                { "excess_initial_margin", SafeGet(acc, AccountItem.ExcessInitialMargin) },
                { "open_positions", PositionsOf(acc).Length },
                { "working_orders", OrdersOf(acc).Count(IsActive) },
                { "executions_in_session", ExecutionsOf(acc).Length },
                { "connected", acc.Connection != null && acc.Connection.Status == ConnectionStatus.Connected }
            };
        }

        // Todos los AccountItem que el proveedor expone (margenes, comisiones, SOD, etc.).
        private object CmdAccountItems(Dictionary<string, object> a)
        {
            Account acc = ResolveAccount(Str(a, "account", null));
            var items = new Dictionary<string, object>();
            foreach (AccountItem item in Enum.GetValues(typeof(AccountItem)))
            {
                try { items[item.ToString()] = acc.Get(item, acc.Denomination); }
                catch { }
            }
            return new Dictionary<string, object>
            {
                { "account", acc.Name }, { "currency", acc.Denomination.ToString() }, { "items", items }
            };
        }

        private IEnumerable<Account> AccountsFor(string name)
        {
            if (string.IsNullOrEmpty(name) || name.Equals("all", StringComparison.OrdinalIgnoreCase) || name == "*")
                return Account.All.ToList();
            return new List<Account> { ResolveAccount(name) };
        }

        private object CmdPositions(Dictionary<string, object> a)
        {
            string name = Str(a, "account", "all");
            var list = new List<object>();
            foreach (Account acc in AccountsFor(name))
            {
                foreach (Position p in PositionsOf(acc))
                {
                    list.Add(new Dictionary<string, object>
                    {
                        { "account", acc.Name },
                        { "instrument", p.Instrument.FullName },
                        { "market_position", p.MarketPosition.ToString() },
                        { "quantity", p.Quantity },
                        { "average_price", p.AveragePrice },
                        { "unrealized_pnl", UnrealizedPnL(p) },
                        { "point_value", p.Instrument.MasterInstrument.PointValue }
                    });
                }
            }
            return list;
        }

        // Instantaneas bajo lock: NinjaTrader modifica estas colecciones desde sus propios hilos.
        private static Order[] OrdersOf(Account a)         { lock (a.Orders) return a.Orders.ToArray(); }
        private static Position[] PositionsOf(Account a)   { lock (a.Positions) return a.Positions.ToArray(); }
        private static Execution[] ExecutionsOf(Account a) { lock (a.Executions) return a.Executions.ToArray(); }

        private static bool IsActive(Order o)
        {
            return o.OrderState == OrderState.Working
                || o.OrderState == OrderState.Accepted
                || o.OrderState == OrderState.Submitted
                || o.OrderState == OrderState.Initialized
                || o.OrderState == OrderState.PartFilled
                || o.OrderState == OrderState.TriggerPending
                || o.OrderState == OrderState.ChangePending
                || o.OrderState == OrderState.ChangeSubmitted
                || o.OrderState == OrderState.CancelPending
                || o.OrderState == OrderState.CancelSubmitted;
        }

        private object CmdOrders(Dictionary<string, object> a)
        {
            string name      = Str(a, "account", "all");
            bool activeOnly  = Bool(a, "active_only", true);
            int limit        = Math.Max(1, Math.Min(5000, (int)Num(a, "limit", 100)));
            string insFilter = Str(a, "instrument", null);

            var list = new List<object>();
            foreach (Account acc in AccountsFor(name))
            {
                foreach (Order o in OrdersOf(acc).Reverse())
                {
                    if (activeOnly && !IsActive(o)) continue;
                    if (insFilter != null && o.Instrument.FullName.IndexOf(insFilter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    list.Add(SerializeOrder(acc, o));
                    if (list.Count >= limit) return list;
                }
            }
            return list;
        }

        private Dictionary<string, object> SerializeOrder(Account acc, Order o)
        {
            return new Dictionary<string, object>
            {
                { "account", acc != null ? acc.Name : null },
                { "order_id", o.OrderId },
                { "name", o.Name },
                { "instrument", o.Instrument.FullName },
                { "action", o.OrderAction.ToString() },
                { "type", o.OrderType.ToString() },
                { "state", o.OrderState.ToString() },
                { "quantity", o.Quantity },
                { "filled", o.Filled },
                { "limit_price", o.LimitPrice },
                { "stop_price", o.StopPrice },
                { "average_fill_price", o.AverageFillPrice },
                { "oco", o.Oco },
                { "tif", o.TimeInForce.ToString() },
                { "from_entry_signal", GetMemberSafe(o, "FromEntrySignal") as string },
                { "time", o.Time.ToString("o", CultureInfo.InvariantCulture) }
            };
        }

        private object CmdExecutions(Dictionary<string, object> a)
        {
            string name      = Str(a, "account", "all");
            int limit        = Math.Max(1, Math.Min(10000, (int)Num(a, "limit", 50)));
            string insFilter = Str(a, "instrument", null);
            DateTime since   = ParseDateOr(Str(a, "since", null), DateTime.MinValue);

            var list = new List<object>();
            foreach (Account acc in AccountsFor(name))
            {
                foreach (Execution e in ExecutionsOf(acc).Reverse())
                {
                    if (e.Time < since) continue;
                    if (insFilter != null && e.Instrument.FullName.IndexOf(insFilter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    var d = SerializeExecution(e);
                    d["account"] = acc.Name;
                    list.Add(d);
                    if (list.Count >= limit) return list;
                }
            }
            return list;
        }

        private object CmdConnections()
        {
            var list = new List<object>();
            foreach (Connection c in Connection.Connections.ToList())
            {
                list.Add(new Dictionary<string, object>
                {
                    { "name", c.Options != null ? c.Options.Name : null },
                    { "provider", c.Options != null ? ToText(GetMemberSafe(c.Options, "Provider")) : null },
                    { "status", c.Status.ToString() },
                    { "price_status", c.PriceStatus.ToString() },
                    { "accounts", c.Accounts.Select(x => (object)x.Name).ToList() }
                });
            }
            return list;
        }

        // ---------------- Instrumentos ----------------
        private object CmdInstrumentInfo(Dictionary<string, object> a)
        {
            Instrument ins = ResolveInstrumentOrThrow(Str(a, "instrument", null));
            MasterInstrument mi = ins.MasterInstrument;
            return new Dictionary<string, object>
            {
                { "full_name", ins.FullName },
                { "master", mi.Name },
                { "description", mi.Description },
                { "type", mi.InstrumentType.ToString() },
                { "tick_size", mi.TickSize },
                { "point_value", mi.PointValue },
                { "tick_value", mi.TickSize * mi.PointValue },
                { "currency", mi.Currency.ToString() },
                { "expiry", ins.Expiry.ToString("yyyy-MM-dd") },
                { "exchange", ins.Exchange.ToString() },
                { "trading_hours", mi.TradingHours != null ? mi.TradingHours.Name : null },
                { "url", GetMemberSafe(mi, "Url") as string },
                { "contracts", Instrument.All.Where(i => i.MasterInstrument == mi && i.Expiry.Date >= DateTime.Now.Date.AddDays(-400))
                                  .OrderBy(i => i.Expiry).Select(i => (object)(i.FullName + " (" + i.Expiry.ToString("yyyy-MM-dd") + ")")).ToList() }
            };
        }

        private object CmdInstrumentSearch(Dictionary<string, object> a)
        {
            string q    = Str(a, "query", "");
            string type = Str(a, "type", null);
            int limit   = Math.Max(1, Math.Min(500, (int)Num(a, "limit", 50)));
            var list = new List<object>();
            lock (MasterInstrument.Sync)
            {
                foreach (MasterInstrument m in MasterInstrument.All)
                {
                    if (m == null) continue;
                    if (type != null && !m.InstrumentType.ToString().Equals(type, StringComparison.OrdinalIgnoreCase)) continue;
                    bool hit = q.Length == 0
                        || m.Name.IndexOf(q, StringComparison.OrdinalIgnoreCase) >= 0
                        || (m.Description != null && m.Description.IndexOf(q, StringComparison.OrdinalIgnoreCase) >= 0);
                    if (!hit) continue;
                    list.Add(new Dictionary<string, object>
                    {
                        { "name", m.Name }, { "description", m.Description },
                        { "type", m.InstrumentType.ToString() }, { "tick_size", m.TickSize },
                        { "point_value", m.PointValue }, { "currency", m.Currency.ToString() },
                        { "trading_hours", m.TradingHours != null ? m.TradingHours.Name : null }
                    });
                    if (list.Count >= limit) break;
                }
            }
            return list;
        }

        private object CmdTradingHours(Dictionary<string, object> a)
        {
            string name = Str(a, "name", null);
            string insName = Str(a, "instrument", null);
            TradingHours th = null;
            if (!string.IsNullOrEmpty(name)) th = TradingHours.Get(name);
            else if (!string.IsNullOrEmpty(insName)) th = ResolveInstrumentOrThrow(insName).MasterInstrument.TradingHours;
            if (th == null)
            {
                var names = new List<object>();
                try
                {
                    var all = GetStaticMember(typeof(TradingHours), "All") as System.Collections.IEnumerable;
                    if (all != null) foreach (object t in all) names.Add(ToText(GetMemberSafe(t, "Name")));
                }
                catch { }
                return new Dictionary<string, object> { { "templates", names } };
            }
            return new Dictionary<string, object>
            {
                { "name", th.Name },
                { "time_zone", ToText(GetMemberSafe(th, "TimeZone")) },
                { "sessions", ToPlain(GetMemberSafe(th, "Sessions"), 2) },
                { "holidays", ToPlain(GetMemberSafe(th, "Holidays"), 2) },
                { "partial_holidays", ToPlain(GetMemberSafe(th, "PartialHolidays"), 2) }
            };
        }

        // ---------------- Market data ----------------
        private object CmdQuote(Dictionary<string, object> a)
        {
            Instrument ins = ResolveInstrumentOrThrow(Str(a, "instrument", null));
            return QuoteOf(ins, true);
        }

        private object CmdQuotes(Dictionary<string, object> a)
        {
            var raw = a.ContainsKey("instruments") ? a["instruments"] as List<object> : null;
            if (raw == null || raw.Count == 0) throw new Exception("instruments requerido (lista)");
            var result = new List<object>();
            var resolved = new List<Instrument>();
            foreach (object o in raw.Take(50))
            {
                Instrument ins = ResolveInstrument(Convert.ToString(o, CultureInfo.InvariantCulture));
                if (ins == null) { result.Add(new Dictionary<string, object> { { "instrument", o }, { "error", "no encontrado" } }); continue; }
                EnsureSubscribed(ins);
                resolved.Add(ins);
            }
            Thread.Sleep(400);
            foreach (Instrument ins in resolved) result.Add(QuoteOf(ins, false));
            return result;
        }

        private Dictionary<string, object> QuoteOf(Instrument ins, bool wait)
        {
            EnsureSubscribed(ins);
            MarketTick t = null;
            lock (gate) { tickCache.TryGetValue(ins.FullName, out t); }
            if (t == null && wait)
            {
                Thread.Sleep(700);
                lock (gate) { tickCache.TryGetValue(ins.FullName, out t); }
            }
            if (t == null)
            {
                // Sin tick todavia: intenta el snapshot de MarketData
                try
                {
                    var md = ins.MarketData;
                    if (md != null && md.Last != null && md.Last.Price > 0)
                        return new Dictionary<string, object>
                        {
                            { "instrument", ins.FullName }, { "ready", true }, { "source", "snapshot" },
                            { "last", md.Last.Price },
                            { "bid", md.Bid != null ? (object)md.Bid.Price : null },
                            { "ask", md.Ask != null ? (object)md.Ask.Price : null },
                            { "time", md.Last.Time.ToString("o", CultureInfo.InvariantCulture) }
                        };
                }
                catch { }
                return new Dictionary<string, object>
                {
                    { "instrument", ins.FullName }, { "ready", false },
                    { "hint", "Suscripcion iniciada. Repite en 1-2 s; requiere feed de datos en vivo." }
                };
            }
            return new Dictionary<string, object>
            {
                { "instrument", ins.FullName }, { "ready", true }, { "source", "stream" },
                { "last", t.Last }, { "bid", t.Bid }, { "ask", t.Ask },
                { "spread_ticks", (t.Ask > 0 && t.Bid > 0) ? (object)Math.Round((t.Ask - t.Bid) / ins.MasterInstrument.TickSize, 2) : null },
                { "volume", t.Volume },
                { "time", t.Time.ToString("o", CultureInfo.InvariantCulture) }
            };
        }

        // ---------------- Historico ----------------
        private class BarsSpec
        {
            public Instrument Instrument;
            public BarsPeriod Period;
            public DateTime From, To;
            public int Count;
            public bool ByRange;
            public string TradingHoursName;
            public string Describe()
            {
                return Instrument.FullName + " " + Period.Value + " " + Period.BarsPeriodType
                    + (Period.MarketDataType != MarketDataType.Last ? " (" + Period.MarketDataType + ")" : "")
                    + (ByRange ? " " + From.ToString("yyyy-MM-dd") + ".." + To.ToString("yyyy-MM-dd") : " ultimas " + Count);
            }
        }

        private BarsSpec ParseBarsSpec(Dictionary<string, object> a)
        {
            var spec = new BarsSpec();
            spec.Instrument = ResolveInstrumentOrThrow(Str(a, "instrument", null));
            string ptype = Str(a, "period_type", "Minute");
            int pval     = Math.Max(1, (int)Num(a, "period_value", 1));
            BarsPeriodType bpt = ParseBarsPeriodType(ptype);
            var bp = new BarsPeriod { BarsPeriodType = bpt, Value = pval };

            string baseType = Str(a, "base_period_type", null);
            if (!string.IsNullOrEmpty(baseType))
            {
                bp.BaseBarsPeriodType  = ParseBarsPeriodType(baseType);
                bp.BaseBarsPeriodValue = Math.Max(1, (int)Num(a, "base_period_value", 1));
            }
            if (a.ContainsKey("value2")) bp.Value2 = (int)Num(a, "value2", 0);

            string mdt = Str(a, "market_data", "Last");
            MarketDataType md;
            if (!Enum.TryParse(mdt, true, out md)) throw new Exception("market_data invalido: " + mdt + " (Last, Bid, Ask)");
            bp.MarketDataType = md;
            spec.Period = bp;

            string fromS = Str(a, "from", null), toS = Str(a, "to", null);
            if (!string.IsNullOrEmpty(fromS))
            {
                spec.ByRange = true;
                spec.From = ParseDateOr(fromS, DateTime.MinValue);
                spec.To   = string.IsNullOrEmpty(toS) ? DateTime.Now : ParseDateOr(toS, DateTime.Now);
                // 'to' con solo fecha = incluir el dia entero
                if (!string.IsNullOrEmpty(toS) && toS.Trim().Length <= 10) spec.To = spec.To.Date.AddDays(1).AddTicks(-1);
                if (spec.To <= spec.From) throw new Exception("'to' debe ser posterior a 'from'");
            }
            spec.Count = Math.Max(1, (int)Num(a, "count", 100));
            spec.TradingHoursName = Str(a, "trading_hours", null);
            return spec;
        }

        // Pide las barras en el hilo de UI y espera al callback. Devuelve el objeto Bars.
        private Bars RequestBars(BarsSpec spec, int timeoutMs)
        {
            BarsRequest request = spec.ByRange
                ? new BarsRequest(spec.Instrument, spec.From, spec.To)
                : new BarsRequest(spec.Instrument, spec.Count);
            request.BarsPeriod = spec.Period;
            try
            {
                TradingHours th = !string.IsNullOrEmpty(spec.TradingHoursName)
                    ? TradingHours.Get(spec.TradingHoursName) : spec.Instrument.MasterInstrument.TradingHours;
                if (!string.IsNullOrEmpty(spec.TradingHoursName) && th == null)
                    throw new Exception("plantilla de trading hours no encontrada: " + spec.TradingHoursName);
                if (th != null) request.TradingHours = th;
            }
            catch (Exception ex) { if (!string.IsNullOrEmpty(spec.TradingHoursName)) throw ex; }

            var waiter = new ManualResetEvent(false);
            string error = null;
            Bars result = null;

            Action<BarsRequest, ErrorCode, string> onDone = (bars, errorCode, errorMessage) =>
            {
                try
                {
                    if (errorCode != ErrorCode.NoError) error = errorCode + ": " + errorMessage;
                    else result = bars != null ? bars.Bars : null;
                }
                catch (Exception ex) { error = ex.Message; }
                finally { waiter.Set(); }
            };

            // BarsRequest.Request solo devuelve la llamada si se lanza desde el hilo de UI.
            Action fire = delegate
            {
                try { request.Request(onDone); }
                catch (Exception ex) { error = ex.Message; waiter.Set(); }
            };
            if (Globals.MainThreadDispatcher != null && !Globals.MainThreadDispatcher.CheckAccess())
                Globals.MainThreadDispatcher.BeginInvoke(fire);
            else
                fire();

            if (!waiter.WaitOne(timeoutMs))
            {
                try { request.Dispose(); } catch { }
                throw new Exception("timeout solicitando historico (" + (timeoutMs / 1000) + " s). Reduce el rango o usa bars_export.");
            }
            if (error != null) throw new Exception(error);
            if (result == null) throw new Exception("NinjaTrader no devolvio barras");
            return result;
        }

        private static Dictionary<string, object> BarRow(Bars b, int i)
        {
            return new Dictionary<string, object>
            {
                { "time",   b.GetTime(i).ToString("yyyy-MM-ddTHH:mm:ss.fff", CultureInfo.InvariantCulture) },
                { "open",   b.GetOpen(i) },
                { "high",   b.GetHigh(i) },
                { "low",    b.GetLow(i) },
                { "close",  b.GetClose(i) },
                { "volume", b.GetVolume(i) }
            };
        }

        private object CmdBars(Dictionary<string, object> a)
        {
            BarsSpec spec = ParseBarsSpec(a);
            int timeout = Math.Max(10, Math.Min(600, (int)Num(a, "timeout_seconds", 90))) * 1000;
            int offset  = Math.Max(0, (int)Num(a, "offset", 0));
            int limit   = Math.Max(1, Math.Min(MaxBarsInline, (int)Num(a, "limit", spec.ByRange ? 5000 : spec.Count)));

            Bars b = RequestBars(spec, timeout);
            int n = b.Count;
            int from = spec.ByRange ? Math.Min(n, offset) : Math.Max(0, n - spec.Count);
            int to   = Math.Min(n, from + limit);
            var rows = new List<object>();
            for (int i = from; i < to; i++) rows.Add(BarRow(b, i));

            return new Dictionary<string, object>
            {
                { "instrument", spec.Instrument.FullName },
                { "period", spec.Period.Value + " " + spec.Period.BarsPeriodType },
                { "market_data", spec.Period.MarketDataType.ToString() },
                { "trading_hours", b.TradingHours != null ? b.TradingHours.Name : null },
                { "total_available", n },
                { "offset", from },
                { "count", rows.Count },
                { "has_more", to < n },
                { "first", n > 0 ? b.GetTime(0).ToString("o", CultureInfo.InvariantCulture) : null },
                { "last",  n > 0 ? b.GetTime(n - 1).ToString("o", CultureInfo.InvariantCulture) : null },
                { "bars", rows }
            };
        }

        // Exportacion asincrona a CSV (para rangos grandes: meses de tick/segundos).
        // Escribe en Documents\NinjaTrader 8\mcp_data\<file>. Devuelve un job_id.
        private object CmdBarsExport(Dictionary<string, object> a)
        {
            BarsSpec spec = ParseBarsSpec(a);
            string file = Str(a, "file", null);
            if (string.IsNullOrEmpty(file))
                file = spec.Instrument.FullName.Replace(" ", "_") + "_" + spec.Period.Value + spec.Period.BarsPeriodType + "_"
                     + (spec.ByRange ? spec.From.ToString("yyyyMMdd") + "_" + spec.To.ToString("yyyyMMdd") : "last" + spec.Count) + ".csv";
            file = SafeFileName(file);
            if (!file.EndsWith(".csv", StringComparison.OrdinalIgnoreCase)) file += ".csv";
            string dir = Path.Combine(Globals.UserDataDir, "mcp_data");
            Directory.CreateDirectory(dir);
            string path = Path.Combine(dir, file);
            int timeout = Math.Max(30, Math.Min(3600, (int)Num(a, "timeout_seconds", 900))) * 1000;

            string jobId = NewJob("bars_export", spec.Describe());
            Thread t = new Thread(delegate()
            {
                try
                {
                    Bars b = RequestBars(spec, timeout);
                    int n = b.Count;
                    string tmp = path + ".part";
                    using (var w = new StreamWriter(tmp, false, new UTF8Encoding(false)))
                    {
                        w.WriteLine("time,open,high,low,close,volume");
                        for (int i = 0; i < n; i++)
                        {
                            w.Write(b.GetTime(i).ToString("yyyy-MM-dd HH:mm:ss.fff", CultureInfo.InvariantCulture)); w.Write(',');
                            w.Write(b.GetOpen(i).ToString("R", CultureInfo.InvariantCulture)); w.Write(',');
                            w.Write(b.GetHigh(i).ToString("R", CultureInfo.InvariantCulture)); w.Write(',');
                            w.Write(b.GetLow(i).ToString("R", CultureInfo.InvariantCulture)); w.Write(',');
                            w.Write(b.GetClose(i).ToString("R", CultureInfo.InvariantCulture)); w.Write(',');
                            w.WriteLine(b.GetVolume(i).ToString(CultureInfo.InvariantCulture));
                        }
                    }
                    if (File.Exists(path)) File.Delete(path);
                    File.Move(tmp, path);
                    FinishJob(jobId, new Dictionary<string, object>
                    {
                        { "status", n > 0 ? "done" : "empty" }, { "file", path }, { "rows", n },
                        { "first", n > 0 ? b.GetTime(0).ToString("o", CultureInfo.InvariantCulture) : null },
                        { "last",  n > 0 ? b.GetTime(n - 1).ToString("o", CultureInfo.InvariantCulture) : null }
                    });
                    Log("bars_export " + jobId + " -> " + n + " filas en " + file);
                }
                catch (Exception ex)
                {
                    FinishJob(jobId, new Dictionary<string, object> { { "status", "error" }, { "error", Unwrap(ex).Message } });
                }
            }) { IsBackground = true, Name = "MCPBarsExport" };
            t.Start();

            return new Dictionary<string, object>
            {
                { "job_id", jobId }, { "status", "running" }, { "file", path },
                { "note", "Exportando en segundo plano. Consulta job_status(job_id)." }
            };
        }

        // ---------------- Eventos (solo lectura) ----------------
        private void PushEvent(string kind, Dictionary<string, object> data)
        {
            lock (gate)
            {
                data["seq"]  = ++eventSeq;
                data["kind"] = kind;
                if (!data.ContainsKey("time")) data["time"] = DateTime.Now.ToString("o", CultureInfo.InvariantCulture);
                events.AddLast(data);
                while (events.Count > MaxEvents) events.RemoveFirst();
            }
        }

        private object CmdEvents(Dictionary<string, object> a)
        {
            HookAccounts();   // engancha cuentas nuevas que hayan aparecido
            long since = (long)Num(a, "since", 0);
            int limit  = Math.Max(1, Math.Min(2000, (int)Num(a, "limit", 200)));
            string kind = Str(a, "kind", null);
            lock (gate)
            {
                var list = events.Where(e => (long)e["seq"] > since
                                          && (kind == null || string.Equals((string)e["kind"], kind, StringComparison.OrdinalIgnoreCase)))
                                 .Take(limit).Select(e => (object)new Dictionary<string, object>(e)).ToList();
                return new Dictionary<string, object>
                {
                    { "last_seq", eventSeq }, { "count", list.Count }, { "events", list }
                };
            }
        }

        private void OnExecutionUpdate(object sender, ExecutionEventArgs e)
        {
            try
            {
                if (e.Execution == null) return;
                var d = SerializeExecution(e.Execution);
                d["account"] = e.Execution.Account != null ? e.Execution.Account.Name : null;
                PushEvent("execution", d);
            }
            catch { }
        }

        private void OnOrderUpdate(object sender, OrderEventArgs e)
        {
            try
            {
                if (e.Order == null) return;
                var d = new Dictionary<string, object>
                {
                    { "account", e.Order.Account != null ? e.Order.Account.Name : null },
                    { "order_id", e.Order.OrderId }, { "name", e.Order.Name },
                    { "instrument", e.Order.Instrument != null ? e.Order.Instrument.FullName : null },
                    { "action", e.Order.OrderAction.ToString() }, { "type", e.Order.OrderType.ToString() },
                    { "state", e.OrderState.ToString() }, { "quantity", e.Quantity }, { "filled", e.Filled },
                    { "limit_price", e.LimitPrice }, { "stop_price", e.StopPrice },
                    { "average_fill_price", e.AverageFillPrice },
                    { "error", e.Error.ToString() }, { "comment", e.Comment },
                    { "time", e.Time.ToString("o", CultureInfo.InvariantCulture) }
                };
                PushEvent("order", d);
                if (e.OrderState == OrderState.Rejected)
                    Log("RECHAZADA " + e.Order.OrderId + " " + e.Order.Name + ": " + e.Error + " " + e.Comment);
            }
            catch { }
        }

        private void OnPositionUpdate(object sender, PositionEventArgs e)
        {
            try
            {
                if (e.Position == null) return;
                PushEvent("position", new Dictionary<string, object>
                {
                    { "account", e.Position.Account != null ? e.Position.Account.Name : null },
                    { "instrument", e.Position.Instrument != null ? e.Position.Instrument.FullName : null },
                    { "market_position", e.MarketPosition.ToString() }, { "quantity", e.Quantity },
                    { "average_price", e.AveragePrice }, { "operation", e.Operation.ToString() }
                });
            }
            catch { }
        }

        private void OnConnectionStatus(object sender, ConnectionStatusEventArgs e)
        {
            try
            {
                PushEvent("connection", new Dictionary<string, object>
                {
                    { "connection", e.Connection != null && e.Connection.Options != null ? e.Connection.Options.Name : null },
                    { "status", e.Status.ToString() }, { "price_status", e.PriceStatus.ToString() },
                    { "error", e.Error.ToString() }, { "native_error", e.NativeError }
                });
                if (e.Status == ConnectionStatus.Connected) HookAccounts();
            }
            catch { }
        }

        private void OnMarketData(object sender, MarketDataEventArgs e)
        {
            try
            {
                if (e.Instrument == null) return;
                string key = e.Instrument.FullName;
                lock (gate)
                {
                    MarketTick t;
                    if (!tickCache.TryGetValue(key, out t)) { t = new MarketTick(); tickCache[key] = t; }
                    if (e.MarketDataType == MarketDataType.Last)     { t.Last = e.Price; t.Volume = e.Volume; }
                    else if (e.MarketDataType == MarketDataType.Bid) t.Bid = e.Price;
                    else if (e.MarketDataType == MarketDataType.Ask) t.Ask = e.Price;
                    t.Time = e.Time;
                }
            }
            catch { }
        }

        private void EnsureSubscribed(Instrument ins)
        {
            lock (gate)
            {
                if (subscribed.Contains(ins.FullName)) return;
                subscribed.Add(ins.FullName);
            }
            try { ins.MarketData.Update += OnMarketData; }
            catch (Exception ex) { Log("Suscripcion market data fallida: " + ex.Message); }
        }

        // ---------------- Estrategias NinjaScript (solo lectura) ----------------
        private object CmdStrategies(Dictionary<string, object> a)
        {
            string filterAccount = Str(a, "account", null);
            var list = new List<object>();

            foreach (Account acc in Account.All)
            {
                if (!string.IsNullOrEmpty(filterAccount) && filterAccount != "all"
                    && !acc.Name.Equals(filterAccount, StringComparison.OrdinalIgnoreCase)) continue;

                IEnumerable<StrategyBase> strategies = null;
                try { strategies = acc.Strategies; } catch { }
                if (strategies == null) continue;

                foreach (StrategyBase st in strategies.ToList())
                {
                    string instrument = null, position = null;
                    int qty = 0;
                    try { instrument = st.Instrument != null ? st.Instrument.FullName : null; } catch { }
                    try
                    {
                        if (st.Position != null)
                        {
                            position = st.Position.MarketPosition.ToString();
                            qty = st.Position.Quantity;
                        }
                    }
                    catch { }

                    list.Add(new Dictionary<string, object>
                    {
                        { "strategy_id", st.Id.ToString(CultureInfo.InvariantCulture) },
                        { "name", st.Name },
                        { "class", st.GetType().FullName },
                        { "account", acc.Name },
                        { "state", st.State.ToString() },
                        { "enabled", st.State == State.Realtime || st.State == State.Historical },
                        { "instrument", instrument },
                        { "market_position", position },
                        { "quantity", qty }
                    });
                }
            }
            return list;
        }

        private object CmdStrategyTypes(Dictionary<string, object> a)
        {
            string filter = Str(a, "filter", null);
            var list = new List<object>();
            Type baseType = typeof(StrategyBase);
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                foreach (Type t in SafeTypes(asm))
                {
                    if (t.IsAbstract || !baseType.IsAssignableFrom(t) || !t.IsPublic) continue;
                    if (!string.IsNullOrEmpty(filter) && t.Name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    list.Add(new Dictionary<string, object>
                    {
                        { "name", t.Name }, { "full_name", t.FullName }, { "assembly", asm.GetName().Name }
                    });
                }
            }
            return list.OrderBy(x => (string)((Dictionary<string, object>)x)["name"]).ToList();
        }

        // Parametros por defecto (los de [NinjaScriptProperty]) de una clase de estrategia,
        // sin cargarla en ninguna cuenta: instancia desligada.
        private object CmdStrategyDefaults(Dictionary<string, object> a)
        {
            string name = Str(a, "strategy", null);
            if (string.IsNullOrEmpty(name)) throw new Exception("strategy requerido");
            Type t = FindStrategyType(name);
            if (t == null) throw new Exception("no encuentro la estrategia '" + name + "'");
            return RunOnUi(delegate
            {
                StrategyBase st = (StrategyBase)Activator.CreateInstance(t);
                ApplyDefaults(st);
                var parameters = new List<object>();
                foreach (PropertyInfo p in t.GetProperties(BindingFlags.Public | BindingFlags.Instance))
                {
                    bool isParam = p.GetCustomAttributes(true).Any(x => x.GetType().Name == "NinjaScriptPropertyAttribute");
                    if (!isParam || !p.CanRead) continue;
                    object v = null;
                    try { v = p.GetValue(st, null); } catch { }
                    parameters.Add(new Dictionary<string, object>
                    {
                        { "name", p.Name }, { "type", ShortType(p.PropertyType) },
                        { "default", SimpleValue(v) }, { "display", PropDisplayName(p) }
                    });
                }
                var common = new Dictionary<string, object>();
                foreach (string k in new[] { "Calculate", "BarsRequiredToTrade", "EntriesPerDirection", "EntryHandling",
                                             "IsExitOnSessionCloseStrategy", "ExitOnSessionCloseSeconds", "StartBehavior",
                                             "TimeInForce", "Slippage", "OrderFillResolution", "IsInstantiatedOnEachOptimizationIteration" })
                    common[k] = SimpleValue(GetMemberSafe(st, k));
                return (object)new Dictionary<string, object>
                {
                    { "strategy", t.Name }, { "class", t.FullName }, { "display_name", st.Name },
                    { "parameters", parameters }, { "common", common }
                };
            });
        }

        private static string PropDisplayName(PropertyInfo p)
        {
            try
            {
                foreach (object at in p.GetCustomAttributes(true))
                {
                    if (at.GetType().Name != "DisplayAttribute") continue;
                    object n = at.GetType().GetProperty("Name") != null ? at.GetType().GetProperty("Name").GetValue(at, null) : null;
                    if (n != null) return n.ToString();
                }
            }
            catch { }
            return null;
        }

        private static void ApplyDefaults(StrategyBase st)
        {
            // NinjaTrader aplica SetDefaults al crear la instancia desde su UI. Si la
            // estrategia aun no tiene nombre, forzamos ese estado (inofensivo: no hay cuenta).
            try
            {
                if (string.IsNullOrEmpty(st.Name)) st.SetState(State.SetDefaults);
            }
            catch { }
        }

        private object CmdStrategyParams(Dictionary<string, object> a)
        {
            string id = Str(a, "strategy_id", null);
            if (string.IsNullOrEmpty(id)) throw new Exception("strategy_id requerido");

            foreach (Account acc in Account.All)
            {
                IEnumerable<StrategyBase> strategies = null;
                try { strategies = acc.Strategies; } catch { }
                if (strategies == null) continue;

                foreach (StrategyBase st in strategies.ToList())
                {
                    if (st.Id.ToString(CultureInfo.InvariantCulture) != id) continue;

                    var parameters = new Dictionary<string, object>();
                    Type type = st.GetType();
                    foreach (PropertyInfo prop in type.GetProperties(BindingFlags.Public | BindingFlags.Instance))
                    {
                        if (prop.DeclaringType != type) continue;
                        if (!prop.CanRead || prop.GetIndexParameters().Length > 0) continue;
                        try { parameters[prop.Name] = SimpleValue(prop.GetValue(st, null)); }
                        catch { }
                    }

                    return new Dictionary<string, object>
                    {
                        { "strategy", st.Name },
                        { "class", type.FullName },
                        { "account", acc.Name },
                        { "state", st.State.ToString() },
                        { "instrument", st.Instrument != null ? st.Instrument.FullName : null },
                        { "parameter_count", parameters.Count },
                        { "parameters", parameters }
                    };
                }
            }
            throw new Exception("estrategia no encontrada: " + id);
        }

        private object CmdLog(Dictionary<string, object> a)
        {
            int limit = (int)Num(a, "limit", 50);
            lock (gate)
                return eventLog.Skip(Math.Max(0, eventLog.Count - limit)).Select(s => (object)s).ToList();
        }

        // ---------------- Jobs ----------------
        private string NewJob(string kind, string description)
        {
            lock (gate)
            {
                string jobId = "job" + (++jobSeq);
                jobs[jobId] = new Dictionary<string, object>
                {
                    { "status", "running" }, { "kind", kind }, { "description", description },
                    { "started", DateTime.Now.ToString("o", CultureInfo.InvariantCulture) }
                };
                // Limita memoria: conserva los 50 jobs mas recientes
                while (jobs.Count > 50)
                {
                    string oldest = jobs.Keys.OrderBy(k => int.Parse(k.Substring(3), CultureInfo.InvariantCulture)).First();
                    jobs.Remove(oldest);
                }
                return jobId;
            }
        }

        private void FinishJob(string jobId, Dictionary<string, object> result)
        {
            lock (gate)
            {
                Dictionary<string, object> job;
                if (!jobs.TryGetValue(jobId, out job)) { job = new Dictionary<string, object>(); jobs[jobId] = job; }
                foreach (var kv in result) job[kv.Key] = kv.Value;
                job["finished"] = DateTime.Now.ToString("o", CultureInfo.InvariantCulture);
            }
        }

        private object CmdJobStatus(Dictionary<string, object> a)
        {
            string jobId = Str(a, "job_id", null);
            if (string.IsNullOrEmpty(jobId)) throw new Exception("job_id requerido");
            lock (gate)
            {
                Dictionary<string, object> job;
                if (!jobs.TryGetValue(jobId, out job))
                    return new Dictionary<string, object> { { "status", "desconocido" }, { "job_id", jobId } };
                var copy = new Dictionary<string, object>(job);
                copy["job_id"] = jobId;
                return copy;
            }
        }

        private object CmdJobs()
        {
            lock (gate)
            {
                var list = new List<object>();
                foreach (var kv in jobs)
                    list.Add(new Dictionary<string, object>
                    {
                        { "job_id", kv.Key },
                        { "kind", kv.Value.ContainsKey("kind") ? kv.Value["kind"] : null },
                        { "status", kv.Value.ContainsKey("status") ? kv.Value["status"] : null },
                        { "description", kv.Value.ContainsKey("description") ? kv.Value["description"] : null }
                    });
                return new Dictionary<string, object> { { "count", list.Count }, { "jobs", list } };
            }
        }

        // =================================================================
        //  Reflexion: LECTURA libre de cualquier objeto de NT8; ESCRITURA
        //  (invocar / asignar / instanciar) solo segun ReflectionPolicy.
        // =================================================================

        private const BindingFlags AllFlags =
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance |
            BindingFlags.Static | BindingFlags.FlattenHierarchy;

        // Tipos sobre los que se puede invocar/asignar aunque no los haya creado el puente.
        private static readonly string[] WritableTypePrefixes =
        {
            "NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.",
            "NinjaTrader.NinjaScript.Optimizers.",
            "NinjaTrader.NinjaScript.OptimizationFitnesses.",
            "NinjaTrader.NinjaScript.PerformanceMetrics.",
            "NinjaTrader.NinjaScript.AddOns.MCPDataDump",
            "NinjaTrader.Data.BarsPeriod",
            "System.String", "System.Math", "System.DateTime", "System.TimeSpan", "System.Convert",
            "System.Text.StringBuilder", "System.Collections.Generic.List`1", "System.Collections.Generic.Dictionary`2",
            "System.Collections.ObjectModel.Collection`1", "System.Collections.ObjectModel.ObservableCollection`1"
        };

        // Tipos que se pueden instanciar con reflect_new (quedan como "propios" del puente).
        private static readonly string[] CreatableTypePrefixes =
        {
            "NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.",
            "NinjaTrader.NinjaScript.Strategies.",
            "NinjaTrader.NinjaScript.Indicators.",
            "NinjaTrader.NinjaScript.Optimizers.",
            "NinjaTrader.NinjaScript.OptimizationFitnesses.",
            "NinjaTrader.NinjaScript.PerformanceMetrics.",
            "NinjaTrader.Data.BarsPeriod",
            "System.Text.StringBuilder", "System.Collections.Generic.List`1", "System.Collections.Generic.Dictionary`2",
            "System.DateTime", "System.TimeSpan"
        };

        // Nunca, en ningun tipo: verbos de ordenes y vias de escape de la reflexion.
        private static readonly string[] DeniedMemberPrefixes =
        {
            "Submit", "Cancel", "Change", "Flatten", "CreateOrder", "Enter", "Exit", "Close", "Reverse",
            "Atm", "StartAtm", "SetState", "Invoke", "DynamicInvoke", "BeginInvoke", "CreateDelegate",
            "InvokeMember", "Execute", "PerformClick", "Raise", "Place", "Buy", "Sell", "Kill", "Shutdown",
            "Compile", "Start", "Stop", "Enable", "Disable", "Connect", "Disconnect", "Delete", "Save",
            "Account", "Connection"
        };

        // Tipos intocables (aunque los hubiera creado el puente).
        private static readonly string[] DeniedTypePrefixes =
        {
            "NinjaTrader.Cbi.", "NinjaTrader.NinjaScript.AtmStrategy", "NinjaTrader.Gui.Chart.ChartTrader",
            "NinjaTrader.Gui.SuperDom", "NinjaTrader.Gui.Tools", "NinjaTrader.Gui.ControlCenter",
            "NinjaTrader.Server", "NinjaTrader.Adapter",
            "System.Reflection.", "System.Delegate", "System.MulticastDelegate", "System.Activator", "System.AppDomain",
            "System.Diagnostics.", "System.IO.", "System.Net.", "System.Runtime.", "System.Type", "System.RuntimeType",
            "System.Windows.Input.", "System.Windows.Automation.", "System.Threading.", "Microsoft.", "System.CodeDom."
        };

        private static bool StartsWithAny(string s, IEnumerable<string> prefixes)
        {
            if (s == null) return false;
            foreach (string p in prefixes)
                if (s.StartsWith(p, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        private bool IsOwned(object o)
        {
            if (o == null) return false;
            lock (gate) return owned.Contains(o);
        }

        // Comprueba si se puede escribir (invocar/asignar) sobre 'target' (o sobre el tipo estatico).
        private void CheckWrite(Type type, object target, string member, bool isSetter)
        {
            string tn = type.FullName ?? type.Name;
            if (StartsWithAny(member, DeniedMemberPrefixes))
                throw new Exception("BLOQUEADO por politica de solo lectura: el miembro '" + member
                    + "' no se puede " + (isSetter ? "asignar" : "invocar") + " por reflexion.");
            if (StartsWithAny(tn, DeniedTypePrefixes) || typeof(Delegate).IsAssignableFrom(type)
                || typeof(MemberInfo).IsAssignableFrom(type))
                throw new Exception("BLOQUEADO por politica de solo lectura: el tipo " + tn + " es de solo lectura por reflexion.");

            // Una estrategia enganchada a una cuenta real/sim es intocable, aunque sea "propia".
            var st = target as StrategyBase;
            if (st != null)
            {
                Account acc = null;
                try { acc = st.Account; } catch { }
                if (acc != null && !acc.Name.Equals("Backtest", StringComparison.OrdinalIgnoreCase))
                    throw new Exception("BLOQUEADO: la estrategia esta asociada a la cuenta " + acc.Name + ".");
            }

            if (target != null && IsOwned(target)) return;
            if (StartsWithAny(tn, WritableTypePrefixes) || StartsWithAny(tn, ExtraWritableTypePrefixes)) return;
            throw new Exception("BLOQUEADO por politica de solo lectura: solo se puede invocar/asignar sobre objetos creados "
                + "con reflect_new o sobre tipos del Strategy Analyzer. '" + tn + "' se puede LEER con reflect_get.");
        }

        private object CmdReflectPolicy()
        {
            return new Dictionary<string, object>
            {
                { "read", "libre: reflect_find, reflect_members, reflect_get sobre cualquier tipo u objeto" },
                { "write", "reflect_call / reflect_set solo sobre objetos creados con reflect_new o tipos permitidos" },
                { "writable_type_prefixes", WritableTypePrefixes.Concat(ExtraWritableTypePrefixes).Select(x => (object)x).ToList() },
                { "creatable_type_prefixes", CreatableTypePrefixes.Select(x => (object)x).ToList() },
                { "denied_member_prefixes", DeniedMemberPrefixes.Select(x => (object)x).ToList() },
                { "denied_type_prefixes", DeniedTypePrefixes.Select(x => (object)x).ToList() },
                { "note", "Politica fija en MCPBridge.cs: no se puede cambiar desde el MCP." }
            };
        }

        // Ejecuta en el hilo de UI de NinjaTrader y devuelve el resultado.
        private object RunOnUi(Func<object> fn) { return RunOnUi(fn, 120000); }

        private object RunOnUi(Func<object> fn, int timeoutMs)
        {
            var disp = Globals.MainThreadDispatcher;
            if (disp == null || disp.CheckAccess()) return fn();

            object result = null;
            Exception error = null;
            var done = new ManualResetEvent(false);
            disp.BeginInvoke(new Action(delegate
            {
                try { result = fn(); }
                catch (Exception ex) { error = ex; }
                finally { done.Set(); }
            }));
            if (!done.WaitOne(timeoutMs)) throw new Exception("timeout esperando al hilo de UI de NinjaTrader");
            if (error != null) throw error;
            return result;
        }

        private string NewHandle(object o)
        {
            lock (gate)
            {
                // Reutiliza el handle si el objeto ya esta registrado
                foreach (var kv in handles) if (ReferenceEquals(kv.Value, o)) return kv.Key;
                string h = "h" + (++handleSeq);
                handles[h] = o;
                return h;
            }
        }

        private object ResolveHandle(string h)
        {
            object o;
            lock (gate)
            {
                if (!handles.TryGetValue(h, out o)) throw new Exception("handle no encontrado: " + h);
            }
            return o;
        }

        private static IEnumerable<Type> SafeTypes(Assembly asm)
        {
            Type[] types;
            try { types = asm.GetTypes(); }
            catch (ReflectionTypeLoadException ex) { types = ex.Types.Where(x => x != null).ToArray(); }
            catch { types = new Type[0]; }
            return types.Where(t => t != null);
        }

        private static Type FindType(string name)
        {
            if (string.IsNullOrEmpty(name)) return null;
            Type t = Type.GetType(name, false, true);
            if (t != null) return t;
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                try { t = asm.GetType(name, false, true); if (t != null) return t; }
                catch { }
            }
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
                foreach (Type ty in SafeTypes(asm))
                {
                    if (ty.FullName == null) continue;
                    if (ty.FullName.Equals(name, StringComparison.OrdinalIgnoreCase)
                        || ty.Name.Equals(name, StringComparison.OrdinalIgnoreCase)) return ty;
                }
            return null;
        }

        private Type FindTypeOrThrow(string name)
        {
            Type t = FindType(name);
            if (t == null) throw new Exception("tipo no encontrado: " + name + " (usa reflect_find para localizarlo)");
            return t;
        }

        // Convierte un valor JSON entrante ({"$handle":..}, {"$type":..,"value":..}
        // o primitivo) a un objeto util antes de coaccionarlo a un tipo destino.
        private object ResolveArg(object v)
        {
            var d = v as Dictionary<string, object>;
            if (d != null)
            {
                if (d.ContainsKey("$handle"))
                {
                    object h = ResolveHandle(Convert.ToString(d["$handle"], CultureInfo.InvariantCulture));
                    if (h is Account || h is Order || h is Position || h is Connection || h is Execution
                        || (h != null && StartsWithAny(h.GetType().FullName, new[] { "NinjaTrader.NinjaScript.AtmStrategy" })))
                        throw new Exception("BLOQUEADO: no se pueden pasar objetos " + h.GetType().Name + " como argumento.");
                    return h;
                }
                if (d.ContainsKey("$type") && d.ContainsKey("value"))
                {
                    Type t = FindTypeOrThrow(Convert.ToString(d["$type"], CultureInfo.InvariantCulture));
                    if (!(t.IsEnum || t.IsPrimitive || t == typeof(string) || t == typeof(decimal)
                          || t == typeof(DateTime) || t == typeof(TimeSpan)))
                        throw new Exception("$type solo admite enums y tipos simples");
                    return CoerceTo(d["value"], t);
                }
            }
            return v;
        }

        private object CoerceTo(object val, Type target)
        {
            val = ResolveArg(val);
            if (target == typeof(object)) return val;
            if (val == null)
                return (target.IsValueType && Nullable.GetUnderlyingType(target) == null)
                    ? Activator.CreateInstance(target) : null;
            if (target.IsInstanceOfType(val)) return val;

            Type nt = Nullable.GetUnderlyingType(target);
            if (nt != null) return CoerceTo(val, nt);

            if (target.IsEnum)
            {
                if (val is string) return Enum.Parse(target, (string)val, true);
                return Enum.ToObject(target, Convert.ToInt64(val, CultureInfo.InvariantCulture));
            }
            if (target == typeof(string)) return Convert.ToString(val, CultureInfo.InvariantCulture);
            if (target == typeof(DateTime))
                return (val is DateTime) ? val
                    : DateTime.Parse(Convert.ToString(val, CultureInfo.InvariantCulture), CultureInfo.InvariantCulture);
            if (target == typeof(TimeSpan))
                return TimeSpan.Parse(Convert.ToString(val, CultureInfo.InvariantCulture), CultureInfo.InvariantCulture);
            if (target == typeof(bool))
            {
                if (val is bool) return val;
                bool b;
                if (bool.TryParse(Convert.ToString(val, CultureInfo.InvariantCulture), out b)) return b;
            }
            if (target.IsPrimitive || target == typeof(decimal))
                return Convert.ChangeType(val, target, CultureInfo.InvariantCulture);
            if (val is IConvertible)
            {
                try { return Convert.ChangeType(val, target, CultureInfo.InvariantCulture); }
                catch { }
            }
            throw new Exception("no puedo convertir '" + val + "' (" + val.GetType().Name + ") a " + target.FullName);
        }

        private object[] BuildArgs(List<object> raw, ParameterInfo[] ps)
        {
            int given = raw != null ? raw.Count : 0;
            object[] result = new object[ps.Length];
            for (int i = 0; i < ps.Length; i++)
            {
                if (typeof(Delegate).IsAssignableFrom(ps[i].ParameterType) && i < given && raw[i] != null)
                    throw new Exception("no se admiten delegados como argumento");
                if (i < given)                 result[i] = CoerceTo(raw[i], ps[i].ParameterType);
                else if (ps[i].IsOptional)     result[i] = ps[i].DefaultValue;
                else                           result[i] = CoerceTo(null, ps[i].ParameterType);
            }
            return result;
        }

        private static bool ParamsFit(ParameterInfo[] ps, int given)
        {
            if (ps.Length < given) return false;
            for (int i = given; i < ps.Length; i++)
                if (!ps[i].IsOptional) return false;
            return true;
        }

        private static List<object> ArgList(Dictionary<string, object> a)
        {
            if (a.ContainsKey("args"))
            {
                var l = a["args"] as List<object>;
                if (l != null) return l;
            }
            return new List<object>();
        }

        private object ResolveTargetAndType(Dictionary<string, object> a, out Type type)
        {
            string h = Str(a, "handle", null);
            if (!string.IsNullOrEmpty(h)) { object o = ResolveHandle(h); type = o.GetType(); return o; }
            string tn = Str(a, "type", null);
            if (!string.IsNullOrEmpty(tn)) { type = FindTypeOrThrow(tn); return null; }
            throw new Exception("indica 'handle' (instancia) o 'type' (miembro estatico)");
        }

        // Serializa el resultado: primitivos tal cual, colecciones acotadas y
        // objetos complejos como handle + ToString para poder seguir operando.
        private object Describe(object v)
        {
            if (v == null) return null;
            Type t = v.GetType();
            if (v is string || v is bool) return v;
            if (t.IsEnum) return v.ToString();
            if (v is DateTime) return ((DateTime)v).ToString("o", CultureInfo.InvariantCulture);
            if (v is TimeSpan) return v.ToString();
            if (t.IsPrimitive || v is decimal) return v;

            if (v is System.Collections.IEnumerable && !(v is System.Collections.IDictionary))
            {
                var items = new List<object>();
                int n = 0;
                foreach (object item in (System.Collections.IEnumerable)v)
                {
                    items.Add(DescribeShallow(item));
                    if (++n >= 200) { items.Add("...(+mas)"); break; }
                }
                return new Dictionary<string, object>
                {
                    { "$handle", NewHandle(v) }, { "$type", t.FullName }, { "count", items.Count }, { "items", items }
                };
            }
            return DescribeShallow(v);
        }

        private object DescribeShallow(object v)
        {
            if (v == null) return null;
            Type t = v.GetType();
            if (v is string || v is bool) return v;
            if (t.IsEnum) return v.ToString();
            if (v is DateTime) return ((DateTime)v).ToString("o", CultureInfo.InvariantCulture);
            if (v is TimeSpan) return v.ToString();
            if (t.IsPrimitive || v is decimal) return v;
            return new Dictionary<string, object>
            {
                { "$handle", NewHandle(v) }, { "$type", t.FullName }, { "toString", SafeToString(v) }
            };
        }

        private static string SafeToString(object v)
        {
            try { string s = v.ToString(); return s != null && s.Length > 300 ? s.Substring(0, 300) : s; }
            catch { return v.GetType().Name; }
        }

        private static string ShortType(Type t)
        {
            if (t == null) return "void";
            if (t == typeof(void)) return "void";
            Type nt = Nullable.GetUnderlyingType(t);
            if (nt != null) return ShortType(nt) + "?";
            if (t.IsGenericType)
            {
                string baseName = t.Name;
                int tick = baseName.IndexOf('`');
                if (tick > 0) baseName = baseName.Substring(0, tick);
                return baseName + "<" + string.Join(", ", t.GetGenericArguments().Select(ShortType)) + ">";
            }
            return t.Name;
        }

        private static string MethodSig(MethodInfo m)
        {
            string ps = string.Join(", ", m.GetParameters().Select(p => ShortType(p.ParameterType) + " " + p.Name));
            return (m.IsStatic ? "static " : "") + ShortType(m.ReturnType) + " " + m.Name + "(" + ps + ")";
        }

        private static bool IsObfuscatedName(string n)
        {
            // Los tipos internos de NT8 estan ofuscados (nombres tipo "9zs=" o "<Ab>"): ruido para una IA.
            return n.EndsWith("=", StringComparison.Ordinal) || n.IndexOf('/') >= 0;
        }

        // -- comandos de reflexion ------------------------------------
        private object CmdReflectFind(Dictionary<string, object> a)
        {
            string ns       = Str(a, "namespace", null);
            string contains = Str(a, "name_contains", null);
            string asmName  = Str(a, "assembly", null);
            int limit       = Math.Max(1, Math.Min(1000, (int)Num(a, "limit", 200)));

            var results = new List<object>();
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                string an = asm.GetName().Name;
                if (asmName != null && an.IndexOf(asmName, StringComparison.OrdinalIgnoreCase) < 0) continue;

                foreach (Type t in SafeTypes(asm))
                {
                    if (t.FullName == null) continue;
                    if (ns != null && (t.Namespace == null
                        || t.Namespace.IndexOf(ns, StringComparison.OrdinalIgnoreCase) < 0)) continue;
                    if (contains != null && t.Name.IndexOf(contains, StringComparison.OrdinalIgnoreCase) < 0) continue;

                    results.Add(new Dictionary<string, object>
                    {
                        { "full_name", t.FullName },
                        { "kind", t.IsEnum ? "enum" : (t.IsInterface ? "interface" : (t.IsValueType ? "struct" : "class")) },
                        { "public", t.IsPublic },
                        { "assembly", an }
                    });
                    if (results.Count >= limit)
                        return new Dictionary<string, object> { { "count", results.Count }, { "truncated", true }, { "types", results } };
                }
            }
            return new Dictionary<string, object> { { "count", results.Count }, { "types", results } };
        }

        private object CmdReflectMembers(Dictionary<string, object> a)
        {
            Type type;
            string h = Str(a, "handle", null);
            if (!string.IsNullOrEmpty(h)) type = ResolveHandle(h).GetType();
            else type = FindTypeOrThrow(Str(a, "type", null));

            BindingFlags bf = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static | BindingFlags.FlattenHierarchy;
            if (Bool(a, "non_public", true)) bf |= BindingFlags.NonPublic;
            bool hideObf = Bool(a, "hide_obfuscated", true);

            string filter = Str(a, "name_contains", null);
            Func<string, bool> keep = name => (filter == null || name.IndexOf(filter, StringComparison.OrdinalIgnoreCase) >= 0)
                                              && !(hideObf && IsObfuscatedName(name));

            var ctors = new List<object>();
            foreach (ConstructorInfo c in type.GetConstructors(bf))
                if (!c.IsStatic)
                    ctors.Add("(" + string.Join(", ", c.GetParameters().Select(p => ShortType(p.ParameterType) + " " + p.Name)) + ")");

            var props = new List<object>();
            foreach (PropertyInfo p in type.GetProperties(bf))
                if (keep(p.Name))
                    props.Add(ShortType(p.PropertyType) + " " + p.Name + (p.CanRead ? " get" : "") + (p.CanWrite ? " set" : ""));

            var methods = new List<object>();
            foreach (MethodInfo m in type.GetMethods(bf))
                if (!m.IsSpecialName && keep(m.Name)) methods.Add(MethodSig(m));

            var fields = new List<object>();
            foreach (FieldInfo f in type.GetFields(bf))
                if (keep(f.Name)) fields.Add(ShortType(f.FieldType) + " " + f.Name + (f.IsStatic ? " [static]" : ""));

            return new Dictionary<string, object>
            {
                { "type", type.FullName },
                { "base", type.BaseType != null ? type.BaseType.FullName : null },
                { "writable_by_policy", StartsWithAny(type.FullName, WritableTypePrefixes) && !StartsWithAny(type.FullName, DeniedTypePrefixes) },
                { "constructors", ctors },
                { "properties", props.Distinct().ToList() },
                { "methods", methods.Distinct().ToList() },
                { "fields", fields }
            };
        }

        private object CmdReflectNew(Dictionary<string, object> a)
        {
            Type type = FindTypeOrThrow(Str(a, "type", null));
            string tn = type.FullName ?? type.Name;
            if (!StartsWithAny(tn, CreatableTypePrefixes) || StartsWithAny(tn, DeniedTypePrefixes)
                || typeof(Delegate).IsAssignableFrom(type))
                throw new Exception("BLOQUEADO por politica: no se puede instanciar " + tn
                    + ". Permitidos: " + string.Join(", ", CreatableTypePrefixes));
            if (typeof(System.Windows.Window).IsAssignableFrom(type) && tn.IndexOf("StrategyAnalyzer", StringComparison.OrdinalIgnoreCase) < 0)
                throw new Exception("BLOQUEADO: solo se pueden abrir ventanas del Strategy Analyzer");

            List<object> raw = ArgList(a);
            bool ui = Bool(a, "ui", false);

            Func<object> make = delegate
            {
                if (raw.Count == 0)
                {
                    try { return Activator.CreateInstance(type, true); }
                    catch (Exception ex) { throw Unwrap(ex); }
                }
                Exception last = null;
                foreach (ConstructorInfo c in type.GetConstructors(AllFlags)
                             .Where(ci => ParamsFit(ci.GetParameters(), raw.Count))
                             .OrderBy(ci => ci.GetParameters().Length))
                {
                    object[] args;
                    try { args = BuildArgs(raw, c.GetParameters()); }
                    catch (Exception ex) { last = ex; continue; }
                    try { return c.Invoke(args); }
                    catch (Exception ex) { throw Unwrap(ex); }
                }
                throw new Exception("ningun constructor de " + type.FullName + " encaja con "
                    + raw.Count + " args" + (last != null ? " (" + last.Message + ")" : ""));
            };

            object instance = ui ? RunOnUi(make) : make();
            if (instance != null) lock (gate) owned.Add(instance);
            return Describe(instance);
        }

        private object CmdReflectGet(Dictionary<string, object> a)
        {
            Type type;
            object target = ResolveTargetAndType(a, out type);
            string name = Str(a, "name", null);
            if (string.IsNullOrEmpty(name)) throw new Exception("name requerido");
            bool ui = Bool(a, "ui", false);

            Func<object> get = delegate
            {
                PropertyInfo p = null;
                try { p = type.GetProperty(name, AllFlags); }
                catch (AmbiguousMatchException) { p = type.GetProperties(AllFlags).FirstOrDefault(x => x.Name == name); }
                if (p != null && p.CanRead && p.GetIndexParameters().Length == 0)
                {
                    MethodInfo g = p.GetGetMethod(true);
                    return p.GetValue((g != null && g.IsStatic) ? null : target, null);
                }
                FieldInfo f = type.GetField(name, AllFlags);
                if (f != null) return f.GetValue(f.IsStatic ? null : target);
                throw new Exception("no existe propiedad o campo '" + name + "' en " + type.FullName);
            };

            try { return Describe(ui ? RunOnUi(get) : get()); }
            catch (Exception ex) { throw Unwrap(ex); }
        }

        private object CmdReflectSet(Dictionary<string, object> a)
        {
            Type type;
            object target = ResolveTargetAndType(a, out type);
            string name = Str(a, "name", null);
            if (string.IsNullOrEmpty(name)) throw new Exception("name requerido");
            CheckWrite(type, target, name, true);
            object raw = a.ContainsKey("value") ? a["value"] : null;
            bool ui = Bool(a, "ui", false);

            Func<object> set = delegate
            {
                PropertyInfo p = null;
                try { p = type.GetProperty(name, AllFlags); }
                catch (AmbiguousMatchException) { p = type.GetProperties(AllFlags).FirstOrDefault(x => x.Name == name); }
                if (p != null && p.CanWrite)
                {
                    if (StartsWithAny(p.PropertyType.FullName, DeniedTypePrefixes) || typeof(Delegate).IsAssignableFrom(p.PropertyType))
                        throw new Exception("BLOQUEADO: no se pueden asignar valores de tipo " + p.PropertyType.FullName);
                    MethodInfo s = p.GetSetMethod(true);
                    p.SetValue((s != null && s.IsStatic) ? null : target, CoerceTo(raw, p.PropertyType), null);
                    return true;
                }
                FieldInfo f = type.GetField(name, AllFlags);
                if (f != null)
                {
                    if (StartsWithAny(f.FieldType.FullName, DeniedTypePrefixes) || typeof(Delegate).IsAssignableFrom(f.FieldType))
                        throw new Exception("BLOQUEADO: no se pueden asignar valores de tipo " + f.FieldType.FullName);
                    f.SetValue(f.IsStatic ? null : target, CoerceTo(raw, f.FieldType));
                    return true;
                }
                throw new Exception("no puedo asignar '" + name + "' en " + type.FullName);
            };

            try { if (ui) RunOnUi(set); else set(); }
            catch (Exception ex) { throw Unwrap(ex); }
            return new Dictionary<string, object> { { "set", true }, { "name", name } };
        }

        private object CmdReflectCall(Dictionary<string, object> a)
        {
            Type type;
            object target = ResolveTargetAndType(a, out type);
            string method = Str(a, "method", null);
            if (string.IsNullOrEmpty(method)) throw new Exception("method requerido");
            CheckWrite(type, target, method, false);
            List<object> raw = ArgList(a);
            bool ui = Bool(a, "ui", false);

            var candidates = type.GetMethods(AllFlags)
                .Where(m => m.Name.Equals(method, StringComparison.OrdinalIgnoreCase)
                         && ParamsFit(m.GetParameters(), raw.Count)
                         && (target != null || m.IsStatic))
                .OrderBy(m => m.GetParameters().Length)
                .ToList();
            if (candidates.Count == 0)
                throw new Exception("no hay metodo '" + method + "' que acepte " + raw.Count + " argumentos en " + type.FullName);

            Exception lastCoerce = null;
            foreach (MethodInfo m in candidates)
            {
                if (m.ContainsGenericParameters) continue;
                object[] args;
                try { args = BuildArgs(raw, m.GetParameters()); }
                catch (Exception ex) { lastCoerce = ex; continue; }

                MethodInfo mm = m;
                object[] aa = args;
                Func<object> call = delegate
                {
                    try { return mm.Invoke(mm.IsStatic ? null : target, aa); }
                    catch (Exception ex) { throw Unwrap(ex); }
                };
                return Describe(ui ? RunOnUi(call) : call());
            }
            throw new Exception("ningun overload de '" + method + "' acepta los argumentos dados"
                + (lastCoerce != null ? " (" + lastCoerce.Message + ")" : ""));
        }

        private object CmdReflectHandles(Dictionary<string, object> a)
        {
            lock (gate)
            {
                var list = new List<object>();
                foreach (var kv in handles)
                    list.Add(new Dictionary<string, object>
                    {
                        { "handle", kv.Key },
                        { "type", kv.Value != null ? kv.Value.GetType().FullName : null },
                        { "owned", kv.Value != null && owned.Contains(kv.Value) },
                        { "toString", kv.Value != null ? SafeToString(kv.Value) : null }
                    });
                return new Dictionary<string, object> { { "count", list.Count }, { "handles", list } };
            }
        }

        private object CmdReflectRelease(Dictionary<string, object> a)
        {
            string h = Str(a, "handle", null);
            lock (gate)
            {
                if (string.IsNullOrEmpty(h) || h.Equals("all", StringComparison.OrdinalIgnoreCase))
                {
                    int n = handles.Count;
                    handles.Clear();
                    owned.Clear();
                    return new Dictionary<string, object> { { "released", n } };
                }
                object o;
                bool ok = handles.TryGetValue(h, out o) && handles.Remove(h);
                if (ok && o != null && !handles.Values.Any(x => ReferenceEquals(x, o))) owned.Remove(o);
                return new Dictionary<string, object> { { "released", ok ? 1 : 0 }, { "handle", h } };
            }
        }

        // Ventanas abiertas. NinjaTrader NO registra sus ventanas en Application.Current.Windows
        // (ahi siempre sale 0): las lleva en Globals.AllWindows.
        private object CmdSaWindows()
        {
            return RunOnUi(delegate
            {
                var list = new List<object>();
                var seen = new HashSet<System.Windows.Window>();
                if (Globals.AllWindows != null)
                    foreach (System.Windows.Window w in Globals.AllWindows)
                        if (w != null && seen.Add(w)) list.Add(DescribeWindow(w));
                System.Windows.Application app = System.Windows.Application.Current;
                if (app != null)
                    foreach (System.Windows.Window w in app.Windows)
                        if (w != null && seen.Add(w)) list.Add(DescribeWindow(w));
                return (object)new Dictionary<string, object> { { "count", list.Count }, { "windows", list } };
            });
        }

        private object DescribeWindow(System.Windows.Window w)
        {
            string title = null;
            try { title = SafeToString(w.Title); } catch { }
            return new Dictionary<string, object>
            {
                { "$handle", NewHandle(w) }, { "type", w.GetType().FullName }, { "title", title }
            };
        }

        // Abre (o reutiliza) la ventana del Strategy Analyzer. Solo esa: abrir otras
        // ventanas (DOM, Chart Trader, Order Entry) queda fuera de la politica.
        private object CmdSaOpen(Dictionary<string, object> a)
        {
            const string needle = "StrategyAnalyzer";
            return RunOnUi(delegate
            {
                if (Globals.AllWindows != null)
                    foreach (System.Windows.Window w in Globals.AllWindows)
                    {
                        if (w == null) continue;
                        if (w.GetType().Name.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0) continue;
                        var found = new Dictionary<string, object>((Dictionary<string, object>)DescribeWindow(w));
                        found["opened"] = false;
                        found["reused"] = true;
                        lock (gate) owned.Add(w);
                        return (object)found;
                    }

                Type winType = FindType("NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzer");
                if (winType == null || !typeof(System.Windows.Window).IsAssignableFrom(winType))
                    throw new Exception("no encuentro la ventana del Strategy Analyzer en esta version de NT8");

                object win;
                try { win = Activator.CreateInstance(winType); }
                catch (Exception ex) { throw new Exception("no puedo instanciar " + winType.FullName + ": " + Unwrap(ex).Message); }

                try { ((System.Windows.Window)win).Show(); } catch { }
                try { ((System.Windows.Window)win).Activate(); } catch { }
                lock (gate) owned.Add(win);

                return (object)new Dictionary<string, object>
                {
                    { "opened", true }, { "$handle", NewHandle(win) }, { "type", winType.FullName }
                };
            });
        }

        private static Exception Unwrap(Exception ex)
        {
            while (ex is TargetInvocationException && ex.InnerException != null) ex = ex.InnerException;
            var agg = ex as AggregateException;
            if (agg != null && agg.InnerExceptions.Count == 1) return Unwrap(agg.InnerExceptions[0]);
            return ex;
        }

        // =================================================================
        //  Backtest asincrono (sa_run) - EXPERIMENTAL
        //
        //  Construye una estrategia desligada (sin cuenta), la configura como lo hace
        //  el Strategy Analyzer (instrumento + InstrumentOrInstrumentList, periodo,
        //  fechas, trading hours) y la ejecuta con el StrategyRunner interno en la
        //  cuenta Backtest. Nunca toca cuentas reales. Un solo backtest a la vez.
        // =================================================================

        private static readonly HashSet<string> ForbiddenStrategyParams = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "Account", "Connection", "Accounts", "Name", "Id"
        };

        private object CmdSaRun(Dictionary<string, object> a)
        {
            string stratName = Str(a, "strategy", null);
            if (string.IsNullOrEmpty(stratName)) throw new Exception("strategy requerido (clase de estrategia; usa strategy_types)");
            string insName = Str(a, "instrument", null);
            if (string.IsNullOrEmpty(insName)) throw new Exception("instrument requerido (p. ej. 'MNQ 12-26')");
            string fromS = Str(a, "from", null), toS = Str(a, "to", null);
            if (string.IsNullOrEmpty(fromS) || string.IsNullOrEmpty(toS)) throw new Exception("from y to requeridos (YYYY-MM-DD)");
            string method = Str(a, "method", "RunBacktest");
            if (!(method == "RunBacktest" || method == "RunStrategyAsync" || method == "RunStrategy"))
                throw new Exception("method debe ser RunBacktest, RunStrategy o RunStrategyAsync");
            bool force = Bool(a, "force", false);

            if (saBusy) throw new Exception("ya hay un backtest en curso; espera a que termine (sa_jobs)");

            // Salvaguarda de estabilidad: el motor interno del Analyzer ha llegado a tumbar NT8.
            // Con posiciones abiertas en cuentas no simuladas, no se arriesga salvo force=true.
            if (!force)
            {
                foreach (Account acc in Account.All)
                {
                    if (acc.Provider == Provider.Simulator || acc.Provider == Provider.Playback) continue;
                    if (acc.Name.Equals("Backtest", StringComparison.OrdinalIgnoreCase)) continue;
                    if (PositionsOf(acc).Length > 0)
                        throw new Exception("hay posiciones abiertas en " + acc.Name + ". sa_run es experimental y podria "
                            + "bloquear NinjaTrader: lanzalo cuando estes plano (o con force=true bajo tu responsabilidad).");
                }
            }

            Type stratType = FindStrategyType(stratName);
            if (stratType == null) throw new Exception("no encuentro la estrategia '" + stratName + "'. Usa strategy_types.");
            Instrument ins = ResolveInstrumentOrThrow(insName);
            BarsPeriodType bpt = ParseBarsPeriodType(Str(a, "period_type", "Minute"));
            int pval = Math.Max(1, (int)Num(a, "period_value", 1));
            DateTime from = ParseDateOr(fromS, DateTime.MinValue), to = ParseDateOr(toS, DateTime.MinValue);
            if (from == DateTime.MinValue || to == DateTime.MinValue || to <= from) throw new Exception("rango de fechas invalido");

            var overrides = a.ContainsKey("params") ? a["params"] as Dictionary<string, object> : null;
            if (overrides != null)
                foreach (var kv in overrides)
                {
                    if (ForbiddenStrategyParams.Contains(kv.Key)) throw new Exception("parametro no permitido: " + kv.Key);
                    if (kv.Value is Dictionary<string, object> || kv.Value is List<object>)
                        throw new Exception("el parametro '" + kv.Key + "' debe ser un valor simple (numero, texto, bool)");
                }

            var applied = new List<object>();
            var failed = new List<object>();
            object strategy = RunOnUi(delegate
            {
                StrategyBase st = (StrategyBase)Activator.CreateInstance(stratType);
                ApplyDefaults(st);
                SetMember(st, "Instrument", ins);
                TrySetMember(st, "InstrumentOrInstrumentList", ins.FullName);
                SetMember(st, "BarsPeriod", new BarsPeriod { BarsPeriodType = bpt, Value = pval });
                SetMember(st, "From", from);
                SetMember(st, "To", to);
                TrySetMember(st, "IncludeTradeHistoryInBacktest", true);
                if (overrides != null)
                    foreach (var kv in overrides)
                        if (TrySetMember(st, kv.Key, kv.Value)) applied.Add(kv.Key); else failed.Add(kv.Key);
                TrySetTradingHours(st);   // su ausencia disparaba un assert que cerraba NT8
                return st;
            });
            lock (gate) owned.Add(strategy);

            object runner = BuildStrategyRunner();
            string jobId = NewJob("backtest", stratName + " " + ins.FullName + " " + pval + " " + bpt + " " + fromS + ".." + toS);
            saBusy = true;

            if (method == "RunStrategyAsync")
            {
                try
                {
                    RunOnUi(delegate
                    {
                        MethodInfo asyncRun = runner.GetType().GetMethods(AllFlags)
                            .FirstOrDefault(m => m.Name == "RunStrategyAsync" && m.GetParameters().Length == 3);
                        if (asyncRun == null) throw new Exception("no encuentro RunStrategyAsync(3 args) en esta version de NT8");
                        Type cbType = asyncRun.GetParameters()[2].ParameterType;
                        SaCallback cb = new SaCallback { Bridge = this, JobId = jobId };
                        Delegate callback = Delegate.CreateDelegate(cbType, cb, typeof(SaCallback).GetMethod("Done"));
                        asyncRun.Invoke(runner, new object[] { strategy, null, callback });
                        return null;
                    });
                }
                catch { saBusy = false; throw; }
            }
            else
            {
                Thread bg = new Thread(delegate() { SaRunWorker(jobId, runner, strategy, method); })
                { IsBackground = true, Name = "MCPSaRun" };
                bg.Start();
            }

            Log("sa_run " + stratName + " " + ins.FullName + " " + fromS + ".." + toS + " (" + method + ") -> " + jobId);
            return new Dictionary<string, object>
            {
                { "job_id", jobId }, { "status", "running" }, { "experimental", true },
                { "strategy", stratName }, { "instrument", ins.FullName }, { "method", method },
                { "params_applied", applied }, { "params_not_found", failed },
                { "note", "Backtest en curso en la cuenta Backtest. Consulta sa_status(job_id)." }
            };
        }

        private void SaRunWorker(string jobId, object runner, object strategy, string method)
        {
            try
            {
                object result = RunOnUi(delegate
                {
                    MethodInfo run = runner.GetType().GetMethods(AllFlags)
                        .Where(m => m.Name == method)
                        .OrderByDescending(m => m.GetParameters().Length)
                        .FirstOrDefault();
                    if (run == null) throw new Exception("no encuentro StrategyRunner." + method + " en esta version de NT8");
                    ParameterInfo[] ps = run.GetParameters();
                    object[] args = new object[ps.Length];
                    for (int i = 0; i < ps.Length; i++)
                    {
                        if (i == 0) args[i] = strategy;
                        else if (ps[i].ParameterType == typeof(bool)) args[i] = true;   // includeTradeHistory
                        else args[i] = ps[i].IsOptional ? ps[i].DefaultValue : null;
                    }
                    return run.Invoke(runner, args);
                }, 900000);
                SaRunFinish(jobId, result);
            }
            catch (Exception ex)
            {
                FinishJob(jobId, new Dictionary<string, object> { { "status", "error" }, { "error", Unwrap(ex).Message } });
                Log("sa_run " + jobId + " ERROR: " + Unwrap(ex).Message);
                saBusy = false;
            }
        }

        private class SaCallback
        {
            public MCPBridge Bridge;
            public string    JobId;
            public void Done(object result) { Bridge.SaRunFinish(JobId, result); }
        }

        // Extrae del RunResult (tipo interno) ejecuciones, operaciones y metricas.
        private void SaRunFinish(string jobId, object result)
        {
            try
            {
                string error = GetMemberSafe(result, "Error") as string;
                var execs = new List<object>();
                var en = GetMemberSafe(result, "Executions") as System.Collections.IEnumerable;
                if (en != null)
                    foreach (object o in en)
                    {
                        Execution e = o as Execution;
                        if (e != null) execs.Add(SerializeExecution(e));
                    }

                // Busca un SystemPerformance en el resultado (o en la estrategia) para las operaciones
                var trades = new List<object>();
                object perf = FindMemberOfType(result, typeof(SystemPerformance));
                if (perf == null)
                {
                    object st = GetMemberSafe(result, "Strategy");
                    if (st != null) perf = GetMemberSafe(st, "SystemPerformance");
                }
                if (perf != null)
                {
                    var all = GetMemberSafe(perf, "AllTrades") as System.Collections.IEnumerable;
                    if (all != null)
                        foreach (object t in all)
                        {
                            trades.Add(SerializeTrade(t));
                            if (trades.Count >= 20000) break;
                        }
                }

                FinishJob(jobId, new Dictionary<string, object>
                {
                    { "status", string.IsNullOrEmpty(error) ? "done" : "error" },
                    { "error", error },
                    { "execution_count", execs.Count },
                    { "executions", execs },
                    { "trade_count", trades.Count },
                    { "trades", trades },
                    { "duration", ToText(GetMemberSafe(result, "Duration")) },
                    { "result_type", result != null ? result.GetType().FullName : null },
                    { "result_members", result != null ? PublicMemberNames(result.GetType()) : null }
                });
                Log("sa_run " + jobId + " terminado: " + execs.Count + " ejecuciones, " + trades.Count + " operaciones");
            }
            catch (Exception ex)
            {
                FinishJob(jobId, new Dictionary<string, object> { { "status", "error" }, { "error", Unwrap(ex).Message } });
                Log("sa_run " + jobId + " ERROR: " + Unwrap(ex).Message);
            }
            finally { saBusy = false; }
        }

        private static List<object> PublicMemberNames(Type t)
        {
            return t.GetProperties(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
                    .Where(p => !IsObfuscatedName(p.Name)).Select(p => (object)(ShortType(p.PropertyType) + " " + p.Name)).ToList();
        }

        private object FindMemberOfType(object o, Type wanted)
        {
            if (o == null) return null;
            foreach (PropertyInfo p in o.GetType().GetProperties(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance))
            {
                if (!wanted.IsAssignableFrom(p.PropertyType) || p.GetIndexParameters().Length > 0) continue;
                try { object v = p.GetValue(o, null); if (v != null) return v; } catch { }
            }
            foreach (FieldInfo f in o.GetType().GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance))
            {
                if (!wanted.IsAssignableFrom(f.FieldType)) continue;
                try { object v = f.GetValue(o); if (v != null) return v; } catch { }
            }
            return null;
        }

        private Dictionary<string, object> SerializeTrade(object t)
        {
            var entry = GetMemberSafe(t, "Entry") as Execution;
            var exit  = GetMemberSafe(t, "Exit") as Execution;
            return new Dictionary<string, object>
            {
                { "trade_number", SimpleValue(GetMemberSafe(t, "TradeNumber")) },
                { "instrument", entry != null && entry.Instrument != null ? entry.Instrument.FullName : null },
                { "direction", entry != null ? entry.MarketPosition.ToString() : null },
                { "quantity", SimpleValue(GetMemberSafe(t, "Quantity")) },
                { "entry_time", entry != null ? entry.Time.ToString("o", CultureInfo.InvariantCulture) : null },
                { "exit_time", exit != null ? exit.Time.ToString("o", CultureInfo.InvariantCulture) : null },
                { "entry_price", entry != null ? (object)entry.Price : null },
                { "exit_price", exit != null ? (object)exit.Price : null },
                { "entry_name", entry != null ? GetMemberSafe(entry, "Name") as string : null },
                { "exit_name", exit != null ? GetMemberSafe(exit, "Name") as string : null },
                { "profit", SimpleValue(GetMemberSafe(t, "ProfitCurrency")) },
                { "commission", SimpleValue(GetMemberSafe(t, "Commission")) },
                { "mae", SimpleValue(GetMemberSafe(t, "MaeCurrency")) },
                { "mfe", SimpleValue(GetMemberSafe(t, "MfeCurrency")) }
            };
        }

        private object BuildStrategyRunner()
        {
            return RunOnUi(delegate
            {
                Type saType     = FindTypeOrThrow("NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzer");
                Type vmType     = FindTypeOrThrow("NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzerViewModel");
                Type runnerType = FindTypeOrThrow("NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyRunner");

                object window = Activator.CreateInstance(saType);
                object vm     = Activator.CreateInstance(vmType, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                                                         null, new object[] { window }, null);
                object runner = Activator.CreateInstance(runnerType, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                                                         null, new object[] { vm }, null);
                lock (gate) { owned.Add(window); owned.Add(vm); owned.Add(runner); }
                return runner;
            });
        }

        private void TrySetTradingHours(object strategy)
        {
            try
            {
                Type vmType = FindType("NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzerViewModel");
                if (vmType == null) return;
                MethodInfo m = vmType.GetMethod("SetStrategyTradingHours", AllFlags);
                if (m != null) m.Invoke(null, new object[] { strategy });
            }
            catch (Exception ex) { Log("trading hours: " + Unwrap(ex).Message); }
        }

        // Tras una compilacion conviven dos ensamblados "NinjaTrader.Custom": el viejo sigue cargado para
        // las instancias que ya corrian. El vigente es el de este AddOn (NinjaTrader lo recarga con cada
        // compilacion); si no esta ahi, el ultimo "NinjaTrader.Custom" cargado. Antes se devolvia el
        // primero, y tras compilar se arrancaba la clase vieja (sin los parametros nuevos).
        private static Type FindStrategyType(string name)
        {
            Type baseType = typeof(StrategyBase);
            Assembly vigente = typeof(MCPBridge).Assembly;
            Type custom = null, fallback = null;
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
                foreach (Type t in SafeTypes(asm))
                {
                    if (t.IsAbstract || !baseType.IsAssignableFrom(t)) continue;
                    if (t.Name.Equals(name, StringComparison.OrdinalIgnoreCase)
                        || (t.FullName != null && t.FullName.Equals(name, StringComparison.OrdinalIgnoreCase)))
                    {
                        if (asm == vigente) return t;
                        if (asm.GetName().Name == "NinjaTrader.Custom") custom = t;   // el ultimo cargado
                        else if (fallback == null) fallback = t;
                    }
                }
            return custom ?? fallback;
        }

        private static BarsPeriodType ParseBarsPeriodType(string ptype)
        {
            BarsPeriodType bpt;
            if (Enum.TryParse(ptype ?? "Minute", true, out bpt)) return bpt;
            throw new Exception("period_type no soportado: " + ptype + ". Validos: "
                + string.Join(", ", Enum.GetNames(typeof(BarsPeriodType))));
        }

        private Dictionary<string, object> SerializeExecution(Execution e)
        {
            double pointValue = 1;
            try { if (e.Instrument != null) pointValue = e.Instrument.MasterInstrument.PointValue; } catch { }
            return new Dictionary<string, object>
            {
                { "execution_id", e.ExecutionId },
                { "instrument", e.Instrument != null ? e.Instrument.FullName : null },
                { "action", e.MarketPosition.ToString() },
                { "quantity", e.Quantity },
                { "price", e.Price },
                { "point_value", pointValue },
                { "time", e.Time.ToString("o", CultureInfo.InvariantCulture) },
                { "commission", e.Commission },
                { "name", GetMemberSafe(e, "Name") as string },
                { "is_entry", SimpleValue(GetMemberSafe(e, "IsEntry")) },
                { "is_exit", SimpleValue(GetMemberSafe(e, "IsExit")) },
                { "order_id", e.Order != null ? e.Order.OrderId : null }
            };
        }

        // ---------------- Utilidades de reflexion segura ----------------
        private static object GetMemberSafe(object o, string name)
        {
            if (o == null) return null;
            try
            {
                PropertyInfo p = o.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (p != null && p.CanRead && p.GetIndexParameters().Length == 0) return p.GetValue(o, null);
                FieldInfo f = o.GetType().GetField(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                return f != null ? f.GetValue(o) : null;
            }
            catch { return null; }
        }

        private static object GetStaticMember(Type t, string name)
        {
            try
            {
                PropertyInfo p = t.GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                if (p != null) return p.GetValue(null, null);
                FieldInfo f = t.GetField(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                return f != null ? f.GetValue(null) : null;
            }
            catch { return null; }
        }

        private void SetMember(object o, string name, object value)
        {
            PropertyInfo p = o.GetType().GetProperty(name, AllFlags);
            if (p != null && p.CanWrite) { p.SetValue(o, CoerceTo(value, p.PropertyType), null); return; }
            FieldInfo f = o.GetType().GetField(name, AllFlags);
            if (f != null) { f.SetValue(o, CoerceTo(value, f.FieldType)); return; }
            throw new Exception("no puedo asignar '" + name + "' en " + o.GetType().FullName);
        }

        private bool TrySetMember(object o, string name, object value)
        {
            try { SetMember(o, name, value); return true; }
            catch { return false; }
        }

        private static string ToText(object v)
        {
            return v == null ? null : SafeToString(v);
        }

        private static object SimpleValue(object v)
        {
            if (v == null) return null;
            Type t = v.GetType();
            if (t.IsEnum) return v.ToString();
            if (v is DateTime) return ((DateTime)v).ToString("o", CultureInfo.InvariantCulture);
            if (v is TimeSpan) return v.ToString();
            if (t.IsPrimitive || v is string || v is decimal) return v;
            return SafeToString(v);
        }

        // Serializacion generica de objetos a diccionarios de valores simples (profundidad limitada).
        private static object ToPlain(object v, int depth)
        {
            if (v == null) return null;
            Type t = v.GetType();
            if (t.IsEnum || t.IsPrimitive || v is string || v is decimal || v is DateTime || v is TimeSpan) return SimpleValue(v);
            if (depth <= 0) return SafeToString(v);
            var dict = v as System.Collections.IDictionary;
            if (dict != null)
            {
                var d = new Dictionary<string, object>();
                int n = 0;
                foreach (System.Collections.DictionaryEntry kv in dict)
                {
                    d[Convert.ToString(SimpleValue(kv.Key), CultureInfo.InvariantCulture)] = ToPlain(kv.Value, depth - 1);
                    if (++n >= 500) break;
                }
                return d;
            }
            var en = v as System.Collections.IEnumerable;
            if (en != null)
            {
                var l = new List<object>();
                foreach (object o in en) { l.Add(ToPlain(o, depth - 1)); if (l.Count >= 500) break; }
                return l;
            }
            var obj = new Dictionary<string, object>();
            foreach (PropertyInfo p in t.GetProperties(BindingFlags.Public | BindingFlags.Instance))
            {
                if (!p.CanRead || p.GetIndexParameters().Length > 0) continue;
                try
                {
                    object pv = p.GetValue(v, null);
                    if (pv == null) { obj[p.Name] = null; continue; }
                    Type pt = pv.GetType();
                    if (pt.IsEnum || pt.IsPrimitive || pv is string || pv is decimal || pv is DateTime || pv is TimeSpan)
                        obj[p.Name] = SimpleValue(pv);
                }
                catch { }
            }
            return obj;
        }

        // ---------------- Utilidades ----------------
        private Account ResolveAccount(string name)
        {
            if (string.IsNullOrEmpty(name))
            {
                Account first = Account.All.FirstOrDefault(x => !x.Name.Equals("Backtest", StringComparison.OrdinalIgnoreCase)
                                                             && !x.Name.Equals("Playback101", StringComparison.OrdinalIgnoreCase))
                                ?? Account.All.FirstOrDefault();
                if (first == null) throw new Exception("no hay cuentas disponibles");
                return first;
            }
            Account acc = Account.All.FirstOrDefault(x => x.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
            if (acc == null) throw new Exception("cuenta no encontrada: " + name);
            return acc;
        }

        private Instrument ResolveInstrumentOrThrow(string name)
        {
            Instrument ins = ResolveInstrument(name);
            if (ins == null) throw new Exception("instrumento no encontrado: " + name + " (usa el nombre completo, p. ej. 'MNQ 12-26', o la raiz 'MNQ')");
            return ins;
        }

        private Instrument ResolveInstrument(string name)
        {
            if (string.IsNullOrEmpty(name)) return null;
            name = name.Trim();
            try
            {
                Instrument direct = Instrument.GetInstrument(name);
                if (direct != null) return direct;
            }
            catch { }

            // Sin mes de vencimiento: contrato activo (el primero no vencido, con rollover a 8 dias).
            try
            {
                MasterInstrument mi = null;
                lock (MasterInstrument.Sync)
                {
                    foreach (MasterInstrument m in MasterInstrument.All)
                        if (m != null && string.Equals(m.Name, name, StringComparison.OrdinalIgnoreCase)) { mi = m; break; }
                }
                if (mi != null)
                {
                    DateTime today = DateTime.Now.Date;
                    Instrument best = null;
                    foreach (Instrument i in Instrument.All)
                    {
                        if (i.MasterInstrument != mi) continue;
                        if (i.Expiry.Date < today.AddDays(8) && mi.InstrumentType == InstrumentType.Future) continue;
                        if (best == null || i.Expiry < best.Expiry) best = i;
                    }
                    if (best != null) return best;
                    if (mi.InstrumentType != InstrumentType.Future)
                    {
                        try { Instrument st = Instrument.GetInstrument(mi.Name); if (st != null) return st; } catch { }
                    }
                }
            }
            catch { }

            try
            {
                Instrument fuzzy = Instrument.GetInstrumentFuzzy(name);
                if (fuzzy != null) return fuzzy;
            }
            catch { }
            return null;
        }

        private static DateTime ParseDateOr(string s, DateTime def)
        {
            if (string.IsNullOrEmpty(s)) return def;
            DateTime d;
            string[] formats = { "yyyy-MM-dd", "yyyy-MM-dd HH:mm", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-ddTHH:mm:ss",
                                 "yyyy-MM-ddTHH:mm", "yyyy-MM-ddTHH:mm:ss.fff", "o" };
            if (DateTime.TryParseExact(s.Trim(), formats, CultureInfo.InvariantCulture, DateTimeStyles.AssumeLocal, out d)) return d;
            if (DateTime.TryParse(s, CultureInfo.InvariantCulture, DateTimeStyles.AssumeLocal, out d)) return d;
            throw new Exception("fecha invalida: " + s + " (usa YYYY-MM-DD o YYYY-MM-DD HH:mm)");
        }

        private static string SafeFileName(string s)
        {
            s = Path.GetFileName(s ?? "");
            foreach (char c in Path.GetInvalidFileNameChars()) s = s.Replace(c, '_');
            if (s.Length == 0 || s.StartsWith(".", StringComparison.Ordinal)) s = "export_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".csv";
            return s;
        }

        private static double SafeGet(Account acc, AccountItem item)
        {
            try { return acc.Get(item, acc.Denomination); }
            catch
            {
                try { return acc.Get(item, Currency.UsDollar); } catch { return 0; }
            }
        }

        private static double UnrealizedPnL(Position p)
        {
            try
            {
                double last = 0;
                if (p.Instrument.MarketData != null && p.Instrument.MarketData.Last != null)
                    last = p.Instrument.MarketData.Last.Price;
                if (last <= 0) return 0;
                return p.GetUnrealizedProfitLoss(PerformanceUnit.Currency, last);
            }
            catch { return 0; }
        }

        private void Log(string msg)
        {
            string line = DateTime.Now.ToString("HH:mm:ss") + "  " + msg;
            lock (gate)
            {
                eventLog.Add(line);
                if (eventLog.Count > MaxLogLines) eventLog.RemoveAt(0);
            }
            try { NinjaTrader.Code.Output.Process("[MCPBridge] " + msg, PrintTo.OutputTab1); } catch { }
        }

        private static string Str(Dictionary<string, object> d, string key, string def)
        {
            object v;
            if (d != null && d.TryGetValue(key, out v) && v != null) return Convert.ToString(v, CultureInfo.InvariantCulture);
            return def;
        }

        private static double Num(Dictionary<string, object> d, string key, double def)
        {
            object v;
            if (d != null && d.TryGetValue(key, out v) && v != null)
            {
                try { return Convert.ToDouble(v, CultureInfo.InvariantCulture); } catch { return def; }
            }
            return def;
        }

        private static bool Bool(Dictionary<string, object> d, string key, bool def)
        {
            object v;
            if (d != null && d.TryGetValue(key, out v) && v != null)
            {
                if (v is bool) return (bool)v;
                bool b;
                if (bool.TryParse(Convert.ToString(v, CultureInfo.InvariantCulture), out b)) return b;
            }
            return def;
        }
    }

    // =================================================================
    //  JSON minimo autocontenido (sin dependencias externas)
    // =================================================================
    internal static class Js
    {
        public static object Parse(string text)
        {
            int i = 0;
            object result = ParseValue(text, ref i, 0);
            SkipWs(text, ref i);
            return result;
        }

        private static void SkipWs(string s, ref int i)
        {
            while (i < s.Length && char.IsWhiteSpace(s[i])) i++;
        }

        private static object ParseValue(string s, ref int i, int depth)
        {
            if (depth > 64) throw new Exception("JSON demasiado anidado");
            SkipWs(s, ref i);
            if (i >= s.Length) throw new Exception("JSON truncado");
            char c = s[i];
            if (c == '{') return ParseObject(s, ref i, depth);
            if (c == '[') return ParseArray(s, ref i, depth);
            if (c == '"') return ParseString(s, ref i);
            if (c == 't') { Expect(s, ref i, "true");  return true; }
            if (c == 'f') { Expect(s, ref i, "false"); return false; }
            if (c == 'n') { Expect(s, ref i, "null");  return null; }
            return ParseNumber(s, ref i);
        }

        private static void Expect(string s, ref int i, string lit)
        {
            if (i + lit.Length > s.Length || s.Substring(i, lit.Length) != lit)
                throw new Exception("JSON invalido en pos " + i);
            i += lit.Length;
        }

        private static Dictionary<string, object> ParseObject(string s, ref int i, int depth)
        {
            var d = new Dictionary<string, object>();
            i++;
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == '}') { i++; return d; }
            while (true)
            {
                SkipWs(s, ref i);
                if (i >= s.Length) throw new Exception("JSON truncado");
                string key = ParseString(s, ref i);
                SkipWs(s, ref i);
                if (i >= s.Length || s[i] != ':') throw new Exception("se esperaba ':'");
                i++;
                d[key] = ParseValue(s, ref i, depth + 1);
                SkipWs(s, ref i);
                if (i >= s.Length) throw new Exception("JSON truncado");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == '}') { i++; return d; }
                throw new Exception("se esperaba ',' o '}'");
            }
        }

        private static List<object> ParseArray(string s, ref int i, int depth)
        {
            var l = new List<object>();
            i++;
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == ']') { i++; return l; }
            while (true)
            {
                l.Add(ParseValue(s, ref i, depth + 1));
                SkipWs(s, ref i);
                if (i >= s.Length) throw new Exception("JSON truncado");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == ']') { i++; return l; }
                throw new Exception("se esperaba ',' o ']'");
            }
        }

        private static string ParseString(string s, ref int i)
        {
            if (i >= s.Length || s[i] != '"') throw new Exception("se esperaba cadena en pos " + i);
            i++;
            var sb = new StringBuilder();
            while (i < s.Length)
            {
                char c = s[i++];
                if (c == '"') return sb.ToString();
                if (c == '\\')
                {
                    if (i >= s.Length) break;
                    char e = s[i++];
                    switch (e)
                    {
                        case '"':  sb.Append('"');  break;
                        case '\\': sb.Append('\\'); break;
                        case '/':  sb.Append('/');  break;
                        case 'b':  sb.Append('\b'); break;
                        case 'f':  sb.Append('\f'); break;
                        case 'n':  sb.Append('\n'); break;
                        case 'r':  sb.Append('\r'); break;
                        case 't':  sb.Append('\t'); break;
                        case 'u':
                            if (i + 4 > s.Length) throw new Exception("escape \\u truncado");
                            sb.Append((char)Convert.ToInt32(s.Substring(i, 4), 16));
                            i += 4;
                            break;
                        default: throw new Exception("escape invalido \\" + e);
                    }
                }
                else sb.Append(c);
            }
            throw new Exception("cadena sin cerrar");
        }

        private static object ParseNumber(string s, ref int i)
        {
            int start = i;
            while (i < s.Length && (char.IsDigit(s[i]) || s[i] == '-' || s[i] == '+' || s[i] == '.'
                                    || s[i] == 'e' || s[i] == 'E')) i++;
            string raw = s.Substring(start, i - start);
            double d;
            if (!double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out d))
                throw new Exception("numero invalido: " + raw);
            return d;
        }

        public static string Write(object value)
        {
            var sb = new StringBuilder();
            WriteValue(sb, value, 0);
            return sb.ToString();
        }

        private static void WriteValue(StringBuilder sb, object v, int depth)
        {
            if (v == null || depth > 64) { sb.Append("null"); return; }

            if (v is string)   { WriteString(sb, (string)v); return; }
            if (v is bool)     { sb.Append((bool)v ? "true" : "false"); return; }
            if (v is DateTime) { WriteString(sb, ((DateTime)v).ToString("o", CultureInfo.InvariantCulture)); return; }
            if (v is Enum)     { WriteString(sb, v.ToString()); return; }

            if (v is double || v is float || v is decimal)
            {
                double d = Convert.ToDouble(v, CultureInfo.InvariantCulture);
                if (double.IsNaN(d) || double.IsInfinity(d) || Math.Abs(d) > 1e300) { sb.Append("null"); return; }
                sb.Append(d.ToString("R", CultureInfo.InvariantCulture));
                return;
            }
            if (v is int || v is long || v is short || v is byte || v is uint || v is ulong || v is ushort || v is sbyte)
            {
                sb.Append(Convert.ToString(v, CultureInfo.InvariantCulture));
                return;
            }

            var dict = v as IDictionary<string, object>;
            if (dict != null)
            {
                sb.Append('{');
                bool first = true;
                foreach (var kv in dict)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    WriteString(sb, kv.Key);
                    sb.Append(':');
                    WriteValue(sb, kv.Value, depth + 1);
                }
                sb.Append('}');
                return;
            }

            var list = v as System.Collections.IEnumerable;
            if (list != null)
            {
                sb.Append('[');
                bool first = true;
                foreach (object item in list)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    WriteValue(sb, item, depth + 1);
                }
                sb.Append(']');
                return;
            }

            WriteString(sb, Convert.ToString(v, CultureInfo.InvariantCulture));
        }

        private static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"':  sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\b': sb.Append("\\b");  break;
                    case '\f': sb.Append("\\f");  break;
                    case '\n': sb.Append("\\n");  break;
                    case '\r': sb.Append("\\r");  break;
                    case '\t': sb.Append("\\t");  break;
                    default:
                        if (c < 32 || c > 126) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }
    }
}

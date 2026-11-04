// WifiTunnelMonitor
// A self-contained Windows service / one-shot checker that keeps this laptop online:
//
//   1. WiFi: keeps the adapter on a PREFERRED SSID (default HomeWiFi_5G) and
//      fails over to a FALLBACK SSID (default HomeWiFi_2.4G) when the
//      preferred one has no internet or the tunnel cannot be reached through it.
//
//   2. Cloudflare Tunnel: probes a public hostname and restarts cloudflared ONLY
//      after SEVERAL consecutive failures AND a cooldown. A single transient
//      Cloudflare 530 / error 1033 no longer triggers an immediate restart, so
//      the monitor no longer causes self-inflicted tunnel outages.
//
// State (failure streak, last restart, last failover) is persisted next to the
// exe so the thresholds also work when the program is run in "once" mode from a
// scheduled task every few minutes.
//
// Compile:
//   csc -nologo -target:exe -out:WifiTunnelMonitor.exe ^
//       -reference:System.dll -reference:System.Core.dll -reference:System.ServiceProcess.dll ^
//       WifiTunnelMonitor.cs

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.ServiceProcess;
using System.Text;
using System.Threading;

namespace WifiTunnelMonitor
{
    public class MonitorService : ServiceBase
    {
        private Thread _worker;
        private volatile bool _running;
        private string _dir;

        // ---- Configuration (overridable via config.ini) ----
        private string _ssid = "HomeWiFi_5G";
        private string _fallbackSsid = "HomeWiFi_2.4G";
        private string _url = "https://cam.example.com";
        private string _cfService = "Cloudflared";
        private int _intervalSeconds = 30;

        // Restart policy: tolerate transient blips before touching cloudflared.
        private int _failThreshold = 3;              // consecutive failures before a restart
        private int _restartCooldownSeconds = 600;   // min seconds between cloudflared restarts
        private int _failoverAfter = 2;              // failures before trying the other SSID
        private int _failoverCooldownSeconds = 900;  // min seconds between SSID failovers
        private int _primaryRecheckSeconds = 1800;   // re-test the preferred SSID this often (0 = never)

        private static readonly TimeSpan TunnelSettle = TimeSpan.FromSeconds(15);
        private const long MaxLogBytes = 5 * 1024 * 1024;
        private const string StateFileName = "monitor-state.ini";

        // ---- Persisted state (survives one-shot runs) ----
        private int _failStreak;
        private DateTime _lastRestartUtc = DateTime.MinValue;
        private DateTime _lastFailoverUtc = DateTime.MinValue;
        private DateTime _lastPrimaryRecheckUtc = DateTime.MinValue;

        public MonitorService()
        {
            this.ServiceName = "WifiTunnelMonitor";
            this.CanStop = true;
            this.CanPauseAndContinue = false;
            this.AutoLog = true;
        }

        // ---------------------------------------------------------------
        static void Main(string[] args)
        {
            MonitorService m = new MonitorService();
            m.LoadConfig();
            m._dir = DataDir();
            m.LoadState();

            if (args.Length > 0 && string.Equals(args[0], "test", StringComparison.OrdinalIgnoreCase))
            {
                m.RunDiagnostics();
                return;
            }
            if (args.Length > 0 && string.Equals(args[0], "once", StringComparison.OrdinalIgnoreCase))
            {
                m.RunOnce();
                return;
            }
            if (args.Length > 0 && string.Equals(args[0], "console", StringComparison.OrdinalIgnoreCase))
            {
                m.RunConsole();
                return;
            }
            ServiceBase.Run(m);
        }

        // ---------------------------------------------------------------
        protected override void OnStart(string[] args)
        {
            try { Directory.CreateDirectory(_dir); } catch { }

            Log("===== Service started =====");
            Log("primary=" + _ssid + "  fallback=" + _fallbackSsid + "  URL=" + _url
                + "  cloudflared=" + _cfService + "  interval=" + _intervalSeconds + "s");

            _running = true;
            _worker = new Thread(WorkerLoop);
            _worker.IsBackground = true;
            _worker.Name = "WifiTunnelMonitorWorker";
            _worker.Start();
        }

        protected override void OnStop()
        {
            Log("===== Service stopping =====");
            _running = false;
            try { if (_worker != null) _worker.Join(5000); }
            catch { }
        }

        // ---------------------------------------------------------------
        private void WorkerLoop()
        {
            while (_running)
            {
                try { Tick(); }
                catch (Exception ex) { Log("ERROR during tick: " + ex.Message); }

                for (int i = 0; i < _intervalSeconds && _running; i++)
                    Thread.Sleep(1000);
            }
        }

        // One-shot mode used by a scheduled task: perform a single check and exit.
        private void RunOnce()
        {
            try { Directory.CreateDirectory(_dir); } catch { }
            Log("===== One-shot check =====");
            try { Tick(); }
            catch (Exception ex) { Log("ERROR during check: " + ex.Message); }
        }

        private void RunConsole()
        {
            try { Directory.CreateDirectory(_dir); } catch { }
            Console.CancelKeyPress += delegate(object s, ConsoleCancelEventArgs e) { _running = false; e.Cancel = true; };
            Log("===== Console run started =====");
            _running = true;
            while (_running)
            {
                try { Tick(); } catch (Exception ex) { Log("ERROR during tick: " + ex.Message); }
                for (int i = 0; i < _intervalSeconds && _running; i++)
                    Thread.Sleep(1000);
            }
            Log("===== Console run stopped =====");
        }

        // ---------------------------------------------------------------
        // One monitoring cycle.
        private void Tick()
        {
            string state, ssid;
            GetWifiState(out state, out ssid);
            Log("WiFi state='" + state + "' ssid='" + ssid + "'");

            // 1. Make sure we are on one of our known networks.
            if (!OnSsid(state, ssid, _ssid) && !OnSsid(state, ssid, _fallbackSsid))
            {
                if (_fallbackSsid.Length == 0 || string.Equals(_fallbackSsid, _ssid, StringComparison.OrdinalIgnoreCase))
                    Log("Not on '" + _ssid + "'; reconnecting...");
                else
                    Log("Not on '" + _ssid + "' or '" + _fallbackSsid + "'; reconnecting...");

                if (!EnsureWifiConnected())
                {
                    Log("Could not connect to '" + _ssid + "'" + (HasFallback ? " or '" + _fallbackSsid + "'" : "") + ".");
                    return;
                }
                GetWifiState(out state, out ssid);
            }

            // 2. While running on the fallback, occasionally re-test the preferred
            //    SSID so we return to the faster 5 GHz network once it recovers.
            if (HasFallback
                && OnSsid(state, ssid, _fallbackSsid)
                && _primaryRecheckSeconds > 0
                && DateTime.UtcNow - _lastPrimaryRecheckUtc > TimeSpan.FromSeconds(_primaryRecheckSeconds))
            {
                _lastPrimaryRecheckUtc = DateTime.UtcNow;
                SaveState();
                Log("Re-testing preferred SSID '" + _ssid + "'...");
                ConnectToSsid(_ssid);
                GetWifiState(out state, out ssid);
                if (OnSsid(state, ssid, _ssid) && HasInternet())
                {
                    Log("Preferred SSID '" + _ssid + "' is working again; staying on it.");
                }
                else
                {
                    Log("Preferred SSID '" + _ssid + "' is still not usable; returning to '" + _fallbackSsid + "'.");
                    ConnectToSsid(_fallbackSsid);
                    GetWifiState(out state, out ssid);
                }
            }

            // 3. No internet? Reconnect the current network once, then fail over.
            if (!HasInternet())
            {
                Log("No internet on '" + ssid + "'. Reconnecting...");
                ConnectToSsid(ssid);
                if (!HasInternet())
                {
                    if (!TryFailover(ref ssid))
                    {
                        Log("No internet on any known SSID; skipping tunnel check.");
                        return;
                    }
                }
            }

            // 4. Check the tunnel.
            CheckTunnel(ssid);
        }

        private bool HasFallback
        {
            get { return _fallbackSsid.Length > 0 && !string.Equals(_fallbackSsid, _ssid, StringComparison.OrdinalIgnoreCase); }
        }

        // ---------------------------------------------------------------
        // WiFi handling
        private bool EnsureWifiConnected()
        {
            if (ConnectToSsid(_ssid)) return true;
            if (HasFallback && ConnectToSsid(_fallbackSsid)) return true;
            return false;
        }

        private bool ConnectToSsid(string ssid)
        {
            Log("Connecting WiFi to '" + ssid + "'...");
            RunProcess("netsh", "wlan connect name=\"" + ssid + "\" ssid=\"" + ssid + "\"");

            string st = "", ss = "";
            for (int i = 0; i < 20; i++)
            {
                Thread.Sleep(1000);
                GetWifiState(out st, out ss);
                if (OnSsid(st, ss, ssid)) return true;
            }
            Log("Could not connect to '" + ssid + "' (state='" + st + "', ssid='" + ss + "').");
            return false;
        }

        // Switch to the other known SSID and report whether it gives internet.
        private bool TryFailover(ref string currentSsid)
        {
            string other = OtherSsid(currentSsid);
            if (other == null)
            {
                Log("No fallback SSID configured; cannot fail over.");
                return false;
            }

            Log("WiFi failover: '" + currentSsid + "' -> '" + other + "'");
            RunProcess("netsh", "wlan connect name=\"" + other + "\" ssid=\"" + other + "\"");

            string st = "", ss = "";
            for (int i = 0; i < 20; i++)
            {
                Thread.Sleep(1000);
                GetWifiState(out st, out ss);
                if (OnSsid(st, ss, other)) break;
            }
            _lastFailoverUtc = DateTime.UtcNow;
            // Don't immediately re-test the preferred SSID; give it time to recover.
            _lastPrimaryRecheckUtc = DateTime.UtcNow;
            SaveState();

            bool connected = OnSsid(st, ss, other);
            bool internet = connected && HasInternet();
            Log("Failover to '" + other + "': connected=" + connected + " internet=" + internet);
            if (internet) currentSsid = other;
            return internet;
        }

        private string OtherSsid(string current)
        {
            if (!HasFallback) return null;
            if (string.Equals(current, _ssid, StringComparison.OrdinalIgnoreCase)) return _fallbackSsid;
            return _ssid;
        }

        // True when the adapter is connected to 'target' (case-insensitive).
        private static bool OnSsid(string state, string currentSsid, string target)
        {
            return target != null && target.Length > 0
                && string.Equals(state, "connected", StringComparison.OrdinalIgnoreCase)
                && currentSsid != null && currentSsid.Length > 0
                && string.Equals(currentSsid, target, StringComparison.OrdinalIgnoreCase);
        }

        private void GetWifiState(out string state, out string ssid)
        {
            state = "";
            ssid = "";
            string outp = RunProcess("netsh", "wlan show interfaces");
            if (outp == null) return;

            string[] lines = outp.Replace("\r\n", "\n").Split('\n');
            foreach (string raw in lines)
            {
                string line = raw.Trim();
                int idx = line.IndexOf(':');
                if (idx <= 0) continue;

                string key = line.Substring(0, idx).Trim();
                string val = line.Substring(idx + 1).Trim();

                if (key.Length == 5 && key.Equals("State", StringComparison.OrdinalIgnoreCase) && state.Length == 0)
                    state = val;
                else if (key.Length == 4 && key.Equals("SSID", StringComparison.OrdinalIgnoreCase) && ssid.Length == 0)
                    ssid = val;
            }
        }

        private static readonly string[] InternetProbes = new string[]
        {
            "1.1.1.1:443", "8.8.8.8:443", "1.1.1.1:53", "8.8.8.8:53", "9.9.9.9:443"
        };

        private bool HasInternet()
        {
            // Consider internet up if ANY well-known endpoint is reachable.
            foreach (string probe in InternetProbes)
            {
                if (TcpProbe(probe, 4000))
                    return true;
            }
            return false;
        }

        private static bool TcpProbe(string hostPort, int timeoutMs)
        {
            try
            {
                int idx = hostPort.LastIndexOf(':');
                string host = hostPort.Substring(0, idx);
                int port = int.Parse(hostPort.Substring(idx + 1));

                using (TcpClient c = new TcpClient())
                {
                    IAsyncResult ar = c.BeginConnect(host, port, null, null);
                    if (!ar.AsyncWaitHandle.WaitOne(timeoutMs, false))
                        return false;
                    c.EndConnect(ar);
                    return c.Connected;
                }
            }
            catch { return false; }
        }

        // ---------------------------------------------------------------
        // Tunnel handling
        private void CheckTunnel(string ssid)
        {
            int status;
            bool reachable;
            string body, err;
            bool healthy = Probe(out status, out reachable, out body, out err);

            if (healthy)
            {
                if (_failStreak > 0)
                    Log("Tunnel is healthy again (HTTP " + status + ").");
                _failStreak = 0;
                SaveState();
                return;
            }

            _failStreak++;
            Log("Tunnel check FAILED (" + _failStreak + "): " + Describe(status, reachable, body, err));

            // Before blaming/restarting cloudflared, try the other WiFi network.
            if (HasFallback
                && _failStreak >= _failoverAfter
                && DateTime.UtcNow - _lastFailoverUtc > TimeSpan.FromSeconds(_failoverCooldownSeconds))
            {
                if (TryFailover(ref ssid))
                {
                    bool healthy2 = Probe(out status, out reachable, out body, out err);
                    Log("After failover to '" + ssid + "': " + (healthy2 ? "tunnel OK" : "still failing (" + Describe(status, reachable, body, err) + ")"));
                    if (healthy2)
                    {
                        _failStreak = 0;
                        SaveState();
                        return;
                    }
                }
            }

            if (_failStreak < _failThreshold)
            {
                SaveState();
                return;
            }

            if (DateTime.UtcNow - _lastRestartUtc < TimeSpan.FromSeconds(_restartCooldownSeconds))
            {
                Log("Cloudflared restart throttled (last restart "
                    + (int)(DateTime.UtcNow - _lastRestartUtc).TotalSeconds + "s ago).");
                SaveState();
                return;
            }

            // Only restart the tunnel if our own origin is actually reachable -
            // otherwise the tunnel is fine and the fault is upstream/on this box.
            if (!LocalOriginOk())
            {
                Log("Origin 127.0.0.1:8880 is not answering either; NOT restarting cloudflared.");
                SaveState();
                return;
            }

            RestartCloudflared();
            _lastRestartUtc = DateTime.UtcNow;
            _failStreak = 0;
            SaveState();
        }

        // Returns true when the tunnel is considered healthy.
        private bool Probe(out int status, out bool reachable, out string body, out string err)
        {
            status = 0;
            reachable = false;
            body = "";
            err = "";

            try
            {
                try { ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12; }
                catch { }

                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(_url);
                req.Method = "GET";
                req.Timeout = 15000;
                req.ReadWriteTimeout = 15000;
                req.AllowAutoRedirect = false;
                req.UserAgent = "WifiTunnelMonitor/1.0";

                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    reachable = true;
                    status = (int)resp.StatusCode;
                    body = ReadLimited(resp, 4096);
                }
            }
            catch (WebException wex)
            {
                if (wex.Response != null)
                {
                    reachable = true;
                    HttpWebResponse resp = (HttpWebResponse)wex.Response;
                    status = (int)resp.StatusCode;
                    body = ReadLimited(resp, 4096);
                    resp.Close();
                }
                else
                {
                    err = wex.Status + ": " + wex.Message;
                }
            }
            catch (Exception ex)
            {
                err = ex.Message;
            }

            // --- decide health ---
            // Cloudflare error 1033 / HTTP 530 => the tunnel itself is not connected.
            if (status == 530 || (body != null && body.IndexOf("1033", StringComparison.Ordinal) >= 0))
                return false;

            // No HTTP response at all => tunnel/edge problem.
            if (!reachable)
                return false;

            // 5xx => origin/edge blip. Treat as unhealthy but let the streak +
            // cooldown gate the restart (a single blip must not restart anything).
            if (status >= 500)
                return false;

            return true;
        }

        // Is our own dashboard/bridge answering locally? Distinguishes an origin
        // fault from a tunnel fault so we never restart cloudflared needlessly.
        private static bool LocalOriginOk()
        {
            try
            {
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:8880/");
                req.Method = "GET";
                req.Timeout = 6000;
                req.ReadWriteTimeout = 6000;
                req.AllowAutoRedirect = false;
                req.UserAgent = "WifiTunnelMonitor/1.0";
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                    return true;
            }
            catch (WebException wex)
            {
                // Any HTTP status (even 4xx/5xx) proves the socket is alive.
                if (wex.Response != null) { wex.Response.Close(); return true; }
                return false;
            }
            catch { return false; }
        }

        private static string ReadLimited(HttpWebResponse resp, int max)
        {
            try
            {
                using (Stream s = resp.GetResponseStream())
                {
                    if (s == null) return "";
                    byte[] buf = new byte[max];
                    int total = 0;
                    while (total < max)
                    {
                        int n = s.Read(buf, total, max - total);
                        if (n <= 0) break;
                        total += n;
                    }
                    return Encoding.UTF8.GetString(buf, 0, total);
                }
            }
            catch { return ""; }
        }

        private static string Describe(int status, bool reachable, string body, string err)
        {
            if (!reachable)
                return "no HTTP response (" + err + ")";
            string extra = "";
            if (body != null && body.IndexOf("1033", StringComparison.Ordinal) >= 0)
                extra = " [Cloudflare error 1033: tunnel not connected]";
            else if (status == 530)
                extra = " [Cloudflare error 530: tunnel/origin unreachable]";
            return "HTTP " + status + extra;
        }

        private void RestartCloudflared()
        {
            Log("Restarting cloudflared service '" + _cfService + "'...");
            try
            {
                using (ServiceController sc = new ServiceController(_cfService))
                {
                    if (sc.Status != ServiceControllerStatus.Stopped)
                    {
                        sc.Stop();
                        sc.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(30));
                    }
                    sc.Start();
                    sc.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(30));
                    Log("Service '" + _cfService + "' is running. Waiting " + (int)TunnelSettle.TotalSeconds + "s for the tunnel to come up...");
                }
                Thread.Sleep(TunnelSettle);
            }
            catch (Exception ex)
            {
                Log("Failed to restart '" + _cfService + "': " + ex.Message);
            }
        }

        // ---------------------------------------------------------------
        private string RunProcess(string file, string args)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(file, args);
                psi.UseShellExecute = false;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                psi.CreateNoWindow = true;

                using (Process p = Process.Start(psi))
                {
                    string outp = p.StandardOutput.ReadToEnd();
                    string errp = p.StandardError.ReadToEnd();
                    p.WaitForExit(20000);
                    return outp + errp;
                }
            }
            catch (Exception ex)
            {
                Log("Failed to run '" + file + "': " + ex.Message);
                return null;
            }
        }

        private void Log(string msg)
        {
            try
            {
                string path = Path.Combine(_dir != null ? _dir : ".", "monitor.log");
                Rotate(path);
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + msg + Environment.NewLine;
                File.AppendAllText(path, line);
            }
            catch { }
        }

        private static void Rotate(string path)
        {
            try
            {
                FileInfo fi = new FileInfo(path);
                if (fi.Exists && fi.Length > MaxLogBytes)
                {
                    string old = path + ".old";
                    if (File.Exists(old)) File.Delete(old);
                    File.Move(path, old);
                }
            }
            catch { }
        }

        // Data (logs + config.ini + state) live next to the executable.
        private static string DataDir()
        {
            try
            {
                string exeDir = AppDomain.CurrentDomain.BaseDirectory;
                if (!string.IsNullOrEmpty(exeDir))
                    return exeDir.TrimEnd('\\');
            }
            catch { }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "WifiTunnelMonitor");
        }

        private void LoadConfig()
        {
            try
            {
                string dir = DataDir();
                string cfg = Path.Combine(dir, "config.ini");
                if (!File.Exists(cfg)) return;

                foreach (string raw in File.ReadAllLines(cfg))
                {
                    string line = raw.Trim();
                    if (line.Length == 0 || line.StartsWith("#") || line.StartsWith(";")) continue;
                    int i = line.IndexOf('=');
                    if (i <= 0) continue;
                    string k = line.Substring(0, i).Trim().ToLowerInvariant();
                    string v = line.Substring(i + 1).Trim();
                    if (v.Length == 0) continue;

                    switch (k)
                    {
                        case "ssid": _ssid = v; break;
                        case "fallback_ssid": _fallbackSsid = v; break;
                        case "url": _url = v; break;
                        case "service": _cfService = v; break;
                        case "interval": _intervalSeconds = IntOr(v, _intervalSeconds, 5); break;
                        case "fail_threshold": _failThreshold = IntOr(v, _failThreshold, 1); break;
                        case "restart_cooldown": _restartCooldownSeconds = IntOr(v, _restartCooldownSeconds, 60); break;
                        case "failover_after": _failoverAfter = IntOr(v, _failoverAfter, 1); break;
                        case "failover_cooldown": _failoverCooldownSeconds = IntOr(v, _failoverCooldownSeconds, 60); break;
                        case "primary_recheck": _primaryRecheckSeconds = IntOr(v, _primaryRecheckSeconds, 0); break;
                    }
                }
            }
            catch { }
        }

        private static int IntOr(string v, int fallback, int min)
        {
            int iv;
            if (int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out iv) && iv >= min)
                return iv;
            return fallback;
        }

        // ---------------------------------------------------------------
        // Persisted state so thresholds/cooldowns work in one-shot mode too.
        private string StatePath
        {
            get { return Path.Combine(_dir != null ? _dir : ".", StateFileName); }
        }

        private void LoadState()
        {
            try
            {
                if (!File.Exists(StatePath)) return;
                foreach (string raw in File.ReadAllLines(StatePath))
                {
                    string line = raw.Trim();
                    int i = line.IndexOf('=');
                    if (i <= 0) continue;
                    string k = line.Substring(0, i).Trim().ToLowerInvariant();
                    string v = line.Substring(i + 1).Trim();

                    if (k == "failstreak")
                    {
                        int n; if (int.TryParse(v, out n)) _failStreak = n;
                    }
                    else if (k == "lastrestartutc")
                    {
                        DateTime d; if (DateTime.TryParse(v, null, DateTimeStyles.RoundtripKind, out d)) _lastRestartUtc = d;
                    }
                    else if (k == "lastfailoverutc")
                    {
                        DateTime d; if (DateTime.TryParse(v, null, DateTimeStyles.RoundtripKind, out d)) _lastFailoverUtc = d;
                    }
                    else if (k == "lastprimaryrecheckutc")
                    {
                        DateTime d; if (DateTime.TryParse(v, null, DateTimeStyles.RoundtripKind, out d)) _lastPrimaryRecheckUtc = d;
                    }
                }
            }
            catch { }
        }

        private void SaveState()
        {
            try
            {
                StringBuilder sb = new StringBuilder();
                sb.AppendLine("failStreak=" + _failStreak);
                sb.AppendLine("lastRestartUtc=" + (_lastRestartUtc == DateTime.MinValue ? "" : _lastRestartUtc.ToString("o")));
                sb.AppendLine("lastFailoverUtc=" + (_lastFailoverUtc == DateTime.MinValue ? "" : _lastFailoverUtc.ToString("o")));
                sb.AppendLine("lastPrimaryRecheckUtc=" + (_lastPrimaryRecheckUtc == DateTime.MinValue ? "" : _lastPrimaryRecheckUtc.ToString("o")));
                File.WriteAllText(StatePath, sb.ToString());
            }
            catch { }
        }

        // ---------------------------------------------------------------
        // `WifiTunnelMonitor.exe test` : one-shot diagnostics, no changes.
        private void RunDiagnostics()
        {
            try { Directory.CreateDirectory(_dir); } catch { }
            Console.WriteLine("Primary SSID     : " + _ssid);
            Console.WriteLine("Fallback SSID    : " + (HasFallback ? _fallbackSsid : "(none)"));
            Console.WriteLine("Tunnel URL       : " + _url);
            Console.WriteLine("cloudflared svc  : " + _cfService);
            Console.WriteLine("Restart policy   : threshold=" + _failThreshold + " cooldown=" + _restartCooldownSeconds + "s"
                              + "  failover_after=" + _failoverAfter + " failover_cooldown=" + _failoverCooldownSeconds + "s"
                              + "  primary_recheck=" + (_primaryRecheckSeconds == 0 ? "off" : _primaryRecheckSeconds + "s"));
            Console.WriteLine();

            string state, ssid;
            GetWifiState(out state, out ssid);
            Console.WriteLine("WiFi state       : " + state);
            Console.WriteLine("WiFi SSID        : " + ssid);
            Console.WriteLine("On primary       : " + OnSsid(state, ssid, _ssid));
            Console.WriteLine("On fallback      : " + OnSsid(state, ssid, _fallbackSsid));
            Console.WriteLine("Internet         : " + HasInternet());
            Console.WriteLine("Origin :8880     : " + LocalOriginOk());

            int status; bool reachable; string body, err;
            bool healthy = Probe(out status, out reachable, out body, out err);
            Console.WriteLine("Tunnel reachable : " + reachable);
            Console.WriteLine("Tunnel HTTP      : " + status);
            Console.WriteLine("Tunnel healthy   : " + healthy);
            Console.WriteLine("Detail           : " + Describe(status, reachable, body, err));

            try
            {
                using (ServiceController sc = new ServiceController(_cfService))
                    Console.WriteLine("cloudflared state: " + sc.Status);
            }
            catch (Exception ex) { Console.WriteLine("cloudflared state: <error " + ex.Message + ">"); }

            Console.WriteLine();
            Console.WriteLine("Persisted state  : failStreak=" + _failStreak
                              + " lastRestart=" + (_lastRestartUtc == DateTime.MinValue ? "never" : _lastRestartUtc.ToString("u"))
                              + " lastFailover=" + (_lastFailoverUtc == DateTime.MinValue ? "never" : _lastFailoverUtc.ToString("u")));
        }
    }
}

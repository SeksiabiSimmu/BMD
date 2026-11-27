using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

// Compiled during setup by installer_core.py using the .NET Framework that ships with Windows.
// Chrome launches this EXE directly. It pipes unmodified binary frames to
// the Python host; no JSON passes through a shell and nothing is printed to stdout.
public static class NativeLauncher {
    public static int Main(string[] args) {
        Process python = null;
        try {
            string root = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);
            string config = Path.Combine(root, "host-paths.txt");
            if (!File.Exists(config)) throw new Exception("host-paths.txt missing; reinstall native host");
            string[] lines = File.ReadAllLines(config, Encoding.UTF8);
            if (lines.Length < 2) throw new Exception("host-paths.txt requires python.exe and native_host.py");
            if (!File.Exists(lines[0]) || !File.Exists(lines[1])) throw new Exception("Python or native host script not found");
            var start = new ProcessStartInfo {
                FileName = lines[0],
                Arguments = "\"" + lines[1].Replace("\"", "") + "\"",
                WorkingDirectory = Path.GetDirectoryName(lines[1]),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            string tools = Path.GetFullPath(Path.Combine(root, "..", "tools"));
            string oldPath = start.EnvironmentVariables["PATH"] ?? "";
            start.EnvironmentVariables["PATH"] = tools + ";" + oldPath;
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            python = Process.Start(start);
            Process child = python;
            Stream browserIn = Console.OpenStandardInput();
            Stream browserOut = Console.OpenStandardOutput();
            // Forward each chunk and FLUSH it. Stream.CopyTo leaves small writes in the pipe's 4 KB buffer
            // until the stream closes, and the browser keeps its end open, so the helper would never
            // receive the first request.
            var send = new Thread(() => {
                try {
                    byte[] buffer = new byte[65536];
                    Stream toChild = child.StandardInput.BaseStream;
                    int count;
                    while ((count = browserIn.Read(buffer, 0, buffer.Length)) > 0) {
                        toChild.Write(buffer, 0, count);
                        toChild.Flush();
                    }
                }
                catch (IOException) { }
                catch (ObjectDisposedException) { }
                finally { try { child.StandardInput.Close(); } catch { } }
            });
            send.IsBackground = true;
            send.Start();
            var errors = new Thread(() => {
                try { child.StandardError.BaseStream.CopyTo(Console.OpenStandardError()); }
                catch (IOException) { }
            });
            errors.IsBackground = true;
            errors.Start();
            try {
                byte[] reply = new byte[65536];
                Stream fromChild = child.StandardOutput.BaseStream;
                int got;
                while ((got = fromChild.Read(reply, 0, reply.Length)) > 0) {
                    browserOut.Write(reply, 0, got);
                    browserOut.Flush();
                }
            }
            finally {
                try { child.WaitForExit(1500); } catch { }
                if (!child.HasExited) { try { child.Kill(); child.WaitForExit(3000); } catch { } }
            }
            return child.ExitCode;
        }
        catch (Exception ex) {
            Console.Error.WriteLine("Brave Music Native Launcher: " + ex.Message);
            if (python != null && !python.HasExited) { try { python.Kill(); } catch { } }
            return 1;
        }
    }
}

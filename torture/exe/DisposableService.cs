// A bounded, disposable SCM service for the observation recipe. Never install
// this under a production service's name or use it with real credentials.
using System;
using System.Collections.Generic;
using System.ServiceProcess;
using System.Threading;

internal sealed class DisposableService : ServiceBase
{
    private readonly ManualResetEvent stopped = new ManualResetEvent(false);
    private readonly List<Thread> workers = new List<Thread>();
    private Timer deadline;
    private byte[] held;
    private byte[] exercised;

    private DisposableService(string name)
    {
        ServiceName = name;
        CanStop = true;
        CanPauseAndContinue = false;
        AutoLog = false;
    }

    protected override void OnStart(string[] args)
    {
        held = new byte[8 * 1024 * 1024];
        for (int offset = 0; offset < held.Length; offset += 4096) held[offset] = 1;
        for (int index = 0; index < 2; index++)
        {
            var worker = new Thread(delegate() { stopped.WaitOne(); });
            worker.IsBackground = true;
            workers.Add(worker);
            worker.Start();
        }
        deadline = new Timer(delegate(object ignored) { Stop(); }, null, 120000, Timeout.Infinite);
    }

    protected override void OnCustomCommand(int command)
    {
        if (command == 128)
        {
            // Repeated exercise replaces this one bounded block, never grows a list.
            var block = new byte[1024 * 1024];
            for (int offset = 0; offset < block.Length; offset += 4096) block[offset] = 2;
            exercised = block;
        }
    }

    protected override void OnStop()
    {
        stopped.Set();
        if (deadline != null) deadline.Dispose();
        foreach (Thread worker in workers) worker.Join(1000);
        GC.KeepAlive(held);
        GC.KeepAlive(exercised);
    }

    private static int Main(string[] args)
    {
        const string Prefix = "DrainTracerRecipe_";
        if (args.Length != 1 || !args[0].StartsWith(Prefix, StringComparison.Ordinal)
            || args[0].Length != Prefix.Length + 32)
            return 2;
        foreach (char character in args[0].Substring(Prefix.Length))
            if (!Uri.IsHexDigit(character)) return 2;
        ServiceBase.Run(new DisposableService(args[0]));
        return 0;
    }
}

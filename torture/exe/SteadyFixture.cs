using System;
using System.Collections.Generic;
using System.Globalization;
using System.Threading;

internal static class SteadyFixture
{
    private static readonly ManualResetEvent Stop = new ManualResetEvent(false);

    private static double ReadDouble(
        string[] args,
        string name,
        double fallback,
        double minimum,
        double maximum)
    {
        for (int index = 0; index + 1 < args.Length; index++)
        {
            if (!String.Equals(args[index], name, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            double parsed;
            if (Double.TryParse(
                args[index + 1],
                NumberStyles.Float,
                CultureInfo.InvariantCulture,
                out parsed))
            {
                return Math.Max(minimum, Math.Min(maximum, parsed));
            }
        }

        return fallback;
    }

    private static int ReadInt(
        string[] args,
        string name,
        int fallback,
        int minimum,
        int maximum)
    {
        for (int index = 0; index + 1 < args.Length; index++)
        {
            if (!String.Equals(args[index], name, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            int parsed;
            if (Int32.TryParse(
                args[index + 1],
                NumberStyles.Integer,
                CultureInfo.InvariantCulture,
                out parsed))
            {
                return Math.Max(minimum, Math.Min(maximum, parsed));
            }
        }

        return fallback;
    }

    private static void HoldWorker()
    {
        Stop.WaitOne();
    }

    private static int Main(string[] args)
    {
        double duration = ReadDouble(args, "--duration", 20.0, 0.1, 300.0);
        int memoryMb = ReadInt(args, "--memory-mb", 8, 0, 256);
        int threadCount = ReadInt(args, "--threads", 2, 0, 32);

        var memory = new List<byte[]>();
        for (int blockIndex = 0; blockIndex < memoryMb; blockIndex++)
        {
            var block = new byte[1024 * 1024];
            for (int offset = 0; offset < block.Length; offset += 4096)
            {
                block[offset] = 1;
            }
            memory.Add(block);
        }

        var workers = new List<Thread>();
        for (int index = 0; index < threadCount; index++)
        {
            var worker = new Thread(HoldWorker);
            worker.IsBackground = true;
            worker.Name = "torture-worker-" + (index + 1).ToString(
                CultureInfo.InvariantCulture);
            worker.Start();
            workers.Add(worker);
        }

        Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs eventArgs)
        {
            eventArgs.Cancel = true;
            Stop.Set();
        };

        DateTime deadline = DateTime.UtcNow.AddSeconds(duration);
        while (DateTime.UtcNow < deadline && !Stop.WaitOne(0))
        {
            Console.WriteLine(
                "steady.exe: {0} MiB, {1} workers",
                memory.Count,
                workers.Count);
            Thread.Sleep(250);
        }

        Stop.Set();
        foreach (Thread worker in workers)
        {
            worker.Join(1000);
        }

        return 0;
    }
}

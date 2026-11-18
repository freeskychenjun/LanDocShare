using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class TrayPeek
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumChildWindows(IntPtr parent, EnumProc cb, IntPtr l);
    delegate bool EnumProc(IntPtr hwnd, IntPtr l);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int GetClassName(IntPtr hwnd, StringBuilder buf, int n);
    [DllImport("user32.dll")]
    static extern IntPtr SendMessage(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")]
    static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("kernel32.dll")]
    static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")]
    static extern IntPtr VirtualAllocEx(IntPtr proc, IntPtr addr, IntPtr size, uint type, uint protect);
    [DllImport("kernel32.dll")]
    static extern bool VirtualFreeEx(IntPtr proc, IntPtr addr, IntPtr size, uint type);
    [DllImport("kernel32.dll")]
    static extern bool ReadProcessMemory(IntPtr proc, IntPtr addr, byte[] buf, IntPtr size, out IntPtr read);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr h);

    const uint TB_GETBUTTON = 0x0417;
    const uint TB_GETRECT = 0x041B;
    const uint TB_BUTTONCOUNT = 0x0418;
    const uint PROCESS_ALL = 0x1F0FFF;
    const uint MEM_COMMIT_RESERVE = 0x3000;
    const uint PAGE_RW = 0x04;
    const uint MEM_RELEASE = 0x8000;

    static string Cls(IntPtr hwnd)
    {
        var sb = new StringBuilder(256);
        GetClassName(hwnd, sb, sb.Capacity);
        return sb.ToString();
    }

    public static List<IntPtr> Toolbars()
    {
        var seen = new HashSet<long>();
        var found = new List<IntPtr>();
        EnumWindows((h, l) =>
        {
            Collect(h, seen, found);
            return true;
        }, IntPtr.Zero);
        found.Sort((a, b) => ButtonCount(b).CompareTo(ButtonCount(a)));
        return found;
    }

    static void Collect(IntPtr h, HashSet<long> seen, List<IntPtr> found)
    {
        if (Cls(h) == "ToolbarWindow32" && seen.Add(h.ToInt64())) found.Add(h);
        EnumChildWindows(h, (c, l) =>
        {
            Collect(c, seen, found);
            return true;
        }, IntPtr.Zero);
    }

    static int ButtonCount(IntPtr tb)
    {
        return (int)SendMessage(tb, TB_BUTTONCOUNT, IntPtr.Zero, IntPtr.Zero);
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X, Y; }
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr h, ref POINT p);

    public static string ButtonRect(IntPtr tb, int index, out int left, out int top, out int right, out int bottom)
    {
        left = top = right = bottom = 0;
        uint pid;
        GetWindowThreadProcessId(tb, out pid);
        IntPtr proc = OpenProcess(PROCESS_ALL, false, pid);
        if (proc == IntPtr.Zero) return "open process failed";
        IntPtr remote = VirtualAllocEx(proc, IntPtr.Zero, (IntPtr)0x1000, MEM_COMMIT_RESERVE, PAGE_RW);
        try
        {
            SendMessage(tb, TB_GETRECT, (IntPtr)index, remote);
            var buf = new byte[16];
            IntPtr read;
            if (!ReadProcessMemory(proc, remote, buf, (IntPtr)16, out read)) return "rpm failed";
            var r = new RECT
            {
                Left = BitConverter.ToInt32(buf, 0),
                Top = BitConverter.ToInt32(buf, 4),
                Right = BitConverter.ToInt32(buf, 8),
                Bottom = BitConverter.ToInt32(buf, 12)
            };
            var p = new POINT { X = r.Left, Y = r.Top };
            ClientToScreen(tb, ref p);
            left = p.X; top = p.Y;
            right = left + (r.Right - r.Left); bottom = top + (r.Bottom - r.Top);
            return "ok";
        }
        finally
        {
            VirtualFreeEx(proc, remote, IntPtr.Zero, MEM_RELEASE);
            CloseHandle(proc);
        }
    }

    public static string Raw(IntPtr tb, int index, int size)
    {
        uint pid;
        GetWindowThreadProcessId(tb, out pid);
        IntPtr proc = OpenProcess(PROCESS_ALL, false, pid);
        if (proc == IntPtr.Zero) return "open process failed";
        IntPtr remote = VirtualAllocEx(proc, IntPtr.Zero, (IntPtr)0x1000, MEM_COMMIT_RESERVE, PAGE_RW);
        try
        {
            SendMessage(tb, TB_GETBUTTON, (IntPtr)index, remote);
            var btn = new byte[32];
            IntPtr read;
            if (!ReadProcessMemory(proc, remote, btn, (IntPtr)32, out read)) return "rpm button failed";
            long dwData = BitConverter.ToInt64(btn, 16);
            if (dwData == 0) return "no TRAYDATA";
            var td = new byte[size];
            if (!ReadProcessMemory(proc, (IntPtr)dwData, td, (IntPtr)size, out read)) return "rpm traydata failed";
            var sb = new StringBuilder();
            sb.AppendFormat("i={0} raw({1})=", index, size);
            for (int k = 0; k < size; k++) sb.Append(td[k].ToString("x2"));
            sb.Append('\n');
            return sb.ToString();
        }
        finally
        {
            VirtualFreeEx(proc, remote, IntPtr.Zero, MEM_RELEASE);
            CloseHandle(proc);
        }
    }

    public static string Dump(IntPtr tb)
    {
        var sb = new StringBuilder();
        uint pid;
        GetWindowThreadProcessId(tb, out pid);
        int count = ButtonCount(tb);
        sb.AppendFormat("toolbar=0x{0:X} pid={1} buttons={2}\n", tb.ToInt64(), pid, count);

        IntPtr proc = OpenProcess(PROCESS_ALL, false, pid);
        if (proc == IntPtr.Zero) { sb.Append("  open process failed\n"); return sb.ToString(); }
        IntPtr remote = VirtualAllocEx(proc, IntPtr.Zero, (IntPtr)0x1000, MEM_COMMIT_RESERVE, PAGE_RW);
        if (remote == IntPtr.Zero) { CloseHandle(proc); sb.Append("  alloc failed\n"); return sb.ToString(); }

        try
        {
            for (int i = 0; i < count; i++)
            {
                if (SendMessage(tb, TB_GETBUTTON, (IntPtr)i, remote) == IntPtr.Zero) { sb.AppendFormat("{0} getbutton failed\n", i); continue; }
                var btn = new byte[32];
                IntPtr read;
                if (!ReadProcessMemory(proc, remote, btn, (IntPtr)32, out read)) { sb.AppendFormat("{0} rpm button failed\n", i); continue; }
                long dwData = BitConverter.ToInt64(btn, 16);
                uint idCommand = BitConverter.ToUInt32(btn, 4);
                if (dwData == 0) { sb.AppendFormat("{0} cmd={1} no TRAYDATA\n", i, idCommand); continue; }

                var td = new byte[48];
                if (!ReadProcessMemory(proc, (IntPtr)dwData, td, (IntPtr)48, out read)) { sb.AppendFormat("{0} rpm traydata failed\n", i); continue; }
                long hwnd = BitConverter.ToInt64(td, 0);
                uint uid = BitConverter.ToUInt32(td, 8);
                uint cb = BitConverter.ToUInt32(td, 12);
                uint flags = BitConverter.ToUInt32(td, 16);
                long hicon = BitConverter.ToInt64(td, 24);
                uint lvpid;
                GetWindowThreadProcessId((IntPtr)hwnd, out lvpid);
                sb.AppendFormat("i={0} uid={1} pid={2} hwnd=0x{3:X} cb=0x{4:X} flags=0x{5:X} hIcon=0x{6:X}\n",
                    i, uid, lvpid, hwnd, cb, flags, hicon);
            }
        }
        finally
        {
            VirtualFreeEx(proc, remote, IntPtr.Zero, MEM_RELEASE);
            CloseHandle(proc);
        }
        return sb.ToString();
    }
}

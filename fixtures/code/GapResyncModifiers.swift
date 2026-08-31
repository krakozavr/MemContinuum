/// Synthetic fixture (finding 6): each `Broken*` struct has the same
/// deliberately unbalanced #if/#else pair as GapDesync.swift's `Broken`
/// (a stray extra '}'), forcing a brace-count desync that (as in
/// GapDesync.swift) triggers right at the struct's own real closing
/// brace. Unlike GapDesync.swift (whose resync anchor is a plain `func`
/// declaring a NEW top-level type), the declaration immediately following
/// each gap here uses vocabulary the gap-resync heuristic did NOT
/// recognize before this fixture: `package`/`consuming`/`borrowing` func
/// modifiers, and bare `var`/`subscript` declarations. Each one must
/// still be recovered as a chunk right after its gap (never swallowed
/// into it, and never skipped past to the next struct's own resync
/// anchor).
struct BrokenPackage {
#if DEBUG
    func broken() {
        print("desynced")
    }
    }
#endif
}

package func afterPackageGap() -> Int {
    return 1
}

struct BrokenConsuming {
#if DEBUG
    func broken() {
        print("desynced")
    }
    }
#endif
}

consuming func afterConsumingGap() -> Int {
    return 2
}

struct BrokenBorrowing {
#if DEBUG
    func broken() {
        print("desynced")
    }
    }
#endif
}

borrowing func afterBorrowingGap() -> Int {
    return 3
}

struct BrokenVar {
#if DEBUG
    func broken() {
        print("desynced")
    }
    }
#endif
}

var afterVarGap: Int {
    return 4
}

struct BrokenSubscript {
#if DEBUG
    func broken() {
        print("desynced")
    }
    }
#endif
}

subscript(i: Int) -> Int {
    return i
}

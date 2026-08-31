import Foundation

/// Synthetic fixture: `Broken`'s #if/#else pair is deliberately missing a
/// brace (one #if branch has an extra stray '}'), causing a mid-file brace
/// desync. The chunker must skip THAT GAP (count + warn) and keep indexing
/// the rest of the file on both sides of it -- `WellFormed.before` (fully
/// closed ahead of the fault) and `AlsoWellFormed.trailing` (resumed after
/// resync) must both still be recovered; never a whole-file fallback.
class WellFormed {
    func before() -> Int {
        return 1
    }
}

class Broken {
#if DEBUG
    func broken() {
        print("desynced")
    }
    }
#endif
    func after() -> Int {
        return 2
    }
}

class AlsoWellFormed {
    func trailing() -> Int {
        return 3
    }
}

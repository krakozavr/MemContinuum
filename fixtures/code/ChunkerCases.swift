/// Synthetic fixture (finding 6): actor-as-container, willSet/didSet
/// exclusion, `extension Outer.Inner` dotted qualifier, and backtick-quoted
/// names -- none exercised by the other chunker fixtures.
actor Counter {
    func increment() -> Int {
        return 1
    }
}

class Observed {
    var value: Int {
        willSet {
            print("will set to \(newValue)")
        }
        didSet {
            print("did set from \(oldValue)")
        }
    }
}

struct Outer {
    struct Inner {}
}

extension Outer.Inner {
    func nested() -> Int {
        return 2
    }
}

class Escaped {
    func `default`() -> Int {
        return 3
    }

    var `type`: Int {
        return 4
    }
}

/// Synthetic fixture (finding 3): `class subscript` is a class-level
/// MEMBER, not a type declaration -- only `class func`/`class var` used to
/// be excluded from the phantom-container check. A backtick-quoted
/// extension target (`` `Type` ``, including a dotted nested one) must
/// still be kept as a real container, not silently dropped.

class Box {
    class subscript(i: Int) -> Int {
        return i
    }

    class final func make() -> Box {
        return Box()
    }
}

struct `Type` {
    struct Inner {}
}

extension `Type` {
    func plain() -> Int {
        return 5
    }
}

extension `Type`.Inner {
    func nested() -> Int {
        return 6
    }
}

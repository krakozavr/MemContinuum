import Foundation

/// Synthetic fixture: `init`, `subscript` (which has no name of its own --
/// symbol is literally "subscript"), and a `static func` operator overload
/// all chunk correctly and are qualified under their containing type.
struct Vec {
    let x: Int

    init(x: Int) {
        self.x = x
    }

    subscript(i: Int) -> Int {
        return x
    }

    static func == (lhs: Vec, rhs: Vec) -> Bool {
        return lhs.x == rhs.x
    }
}

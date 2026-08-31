import Foundation

/// Synthetic fixture for tests/test_code_index.py: nested types (class
/// containing a struct containing a func), an extension adding a method to
/// the outer type, and confirms class/struct themselves are NEVER chunks --
/// only their func members are, qualified through the enclosing-type chain.
class Outer {
    struct Inner {
        func innerFunc() -> Int {
            return 1
        }
    }

    func outerFunc() {
        print("hi")
    }
}

extension Outer {
    func extFunc() {
        print("ext")
    }
}

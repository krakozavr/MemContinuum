import Foundation

/// Synthetic fixture: a balanced `#if`/`#else`/`#endif` is transparent to
/// the brace walker -- both branches are scanned as ordinary sequential
/// code, so BOTH `mode()` definitions are chunked (kept both sides), never
/// just one picked by evaluating the condition.
class Debugger {
#if DEBUG
    func mode() -> String {
        return "debug"
    }
#else
    func mode() -> String {
        return "release"
    }
#endif

    func label() -> String {
        return "Debugger: \(mode())"
    }
}

import Foundation

/// Deliberate fixture bug: this bypasses DeleteGate and removes a file
/// directly, which is exactly the drift TOP-0100/L2's invariant exists to
/// catch. Used by memidx `drift`'s RED case; the GREEN case is a copy of
/// this tree with this call routed through DeleteGate.delete instead.
struct QuickCleanup {
    static func purgeTemp(_ path: String) throws {
        try FileManager.default.removeItem(atPath: path)
    }
}

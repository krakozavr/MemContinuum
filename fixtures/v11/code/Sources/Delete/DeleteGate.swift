import Foundation

/// The single sanctioned call site for removing a file from disk.
struct DeleteGate {
    static func delete(_ path: String) throws {
        try FileManager.default.removeItem(atPath: path)
    }
}

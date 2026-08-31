import Foundation

/// Synthetic fixture: `lazy var x = { ... }()` is a stored property with a
/// closure initializer and must NOT be chunked; `var computed: Int { ... }`
/// has no `=` before its body brace and IS a computed property -- chunked.
class Cache {
    lazy var cached: Int = {
        return 5
    }()

    var computed: Int {
        return cached + 1
    }

    var stored: Int = 5
}

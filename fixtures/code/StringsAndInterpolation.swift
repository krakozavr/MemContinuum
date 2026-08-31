import Foundation

/// Synthetic fixture: a `{`/`}` character sitting inside a plain string
/// literal must never be mistaken for a real brace (it would desync the
/// whole rest of the file if it were); a string interpolation containing a
/// closure (`\(items.map { ... }.joined())`) must balance correctly so the
/// enclosing function's own body-close is still found in the right place.
class Formatter {
    func greet(items: [String]) -> String {
        let template = "hello {world}"
        let joined = "\(items.map { $0.uppercased() }.joined())"
        return template + joined
    }

    func rawPath() -> String {
        let path = #"C:\Users\test "quoted" {braces}"#
        return path
    }

    func multiline() -> String {
        let text = """
        multi {line}
        string with \(1 + 2) interpolation
        """
        return text
    }
}

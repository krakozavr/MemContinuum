// legacy note: func commentedOutSymbol() used to live here, removed in a
// later cleanup -- this comment is the ONLY place that name still appears.
class Real {
    let doc = "example: func stringOnlySymbol() -> Int { return 0 }"

    func realSymbol() -> Int {
        return 1
    }

    static func staticHelper() -> Int {
        return 2
    }

    func `escaped`() -> Int {
        return 3
    }
}

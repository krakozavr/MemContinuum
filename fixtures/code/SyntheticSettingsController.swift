import AppKit

/// Synthetic ~200-line AppKit-style fixture for tests/test_code_index.py.
/// Modeled loosely on the shape of a real settings-window controller
/// (a form of labeled rows built up procedurally, a table data source, a
/// couple of computed properties, an extension for delegate conformance)
/// but written from scratch for this repo -- not copied from any real
/// project file, so this tracked fixture stays generic.
final class SyntheticSettingsController: NSViewController {

    // MARK: - Stored state

    private var rows: [NSView] = []
    private var selectedRowIndex: Int = -1
    private weak var tableView: NSTableView?

    /// The number of configured rows, exposed for the table data source.
    var rowCount: Int {
        return rows.count
    }

    /// The currently selected row's label, or an empty string when nothing
    /// is selected -- a computed property (chunk), not a stored one.
    var selectedLabel: String {
        guard selectedRowIndex >= 0, selectedRowIndex < rows.count else {
            return ""
        }
        return rows[selectedRowIndex].accessibilityLabel() ?? ""
    }

    /// Lazily-built placeholder view; a stored property with a closure
    /// initializer, so it must NOT be chunked (unlike `selectedLabel`
    /// above).
    lazy var placeholderView: NSView = {
        let view = NSView(frame: .zero)
        view.wantsLayer = true
        return view
    }()

    // MARK: - Lifecycle

    override func viewDidLoad() {
        super.viewDidLoad()
        buildRows()
        installTableView()
    }

    override func viewWillAppear() {
        super.viewWillAppear()
        reloadRowLabels()
    }

    // MARK: - Row construction

    private func buildRows() {
        let specs = [
            ("General", "settings.general"),
            ("Processing", "settings.processing"),
            ("Deletion", "settings.deletion"),
            ("Advanced", "settings.advanced"),
        ]
        for (label, key) in specs {
            rows.append(makeRow(label: label, key: key))
        }
    }

    private func makeRow(label: String, key: String) -> NSView {
        let field = NSTextField(labelWithString: label)
        field.setAccessibilityLabel(label)
        field.identifier = NSUserInterfaceItemIdentifier(key)
        return embedInScrollBox(field)
    }

    /// Wraps one row's content view in a scroll container -- a common
    /// AppKit idiom for making a form section independently scrollable.
    func embedInScrollBox(_ content: NSView, inset: CGFloat = 8) -> NSScrollView {
        let scroll = NSScrollView(frame: .zero)
        scroll.hasVerticalScroller = true
        scroll.documentView = content
        content.frame = content.frame.insetBy(dx: -inset, dy: -inset)
        return scroll
    }

    private func installTableView() {
        let table = NSTableView(frame: .zero)
        table.dataSource = self
        table.delegate = self
        self.tableView = table
    }

    private func reloadRowLabels() {
        for (index, row) in rows.enumerated() {
            let label = "\(index): \(row.accessibilityLabel() ?? "unlabeled")"
            row.setAccessibilityLabel(label)
        }
    }

    // MARK: - Selection

    func selectRow(at index: Int) {
        guard index >= 0, index < rows.count else {
            return
        }
        selectedRowIndex = index
        tableView?.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
    }

    func clearSelection() {
        selectedRowIndex = -1
        tableView?.deselectAll(nil)
    }

    // MARK: - Path-length budget checks (mirrors a real-world helper shape)

    /// Whether a would-be destination path exceeds the configured total
    /// character budget -- used before offering a row's action as enabled.
    static func exceedsBudget(_ path: String, limit: Int = 255) -> Bool {
        return path.count > limit
    }

    // MARK: - Debug rendering

    /// Renders this controller's view to a PNG on disk -- named the way a
    /// real debug-render helper would be, so the FTS split-token test
    /// ("write png" finding a "writePNG"-shaped symbol) has a realistic
    /// fixture to search over.
    func writePNGSnapshot(to path: String, tag: String) -> Bool {
        guard let view = self.view as NSView? else {
            return false
        }
        let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds)
        return rep != nil && !path.isEmpty && !tag.isEmpty
    }

    // MARK: - Cleanup

    /// The single gate every teardown path calls, so cleanup side effects
    /// are traceable to one call site regardless of how the controller was
    /// dismissed.
    func cleanup(removeObservers: Bool) -> Bool {
        if removeObservers {
            NotificationCenter.default.removeObserver(self)
        }
        rows.removeAll()
        tableView = nil
        return true
    }

    deinit {
        _ = cleanup(removeObservers: true)
    }
}

// MARK: - Table data source / delegate conformance

extension SyntheticSettingsController: NSTableViewDataSource, NSTableViewDelegate {
    func numberOfRows(in tableView: NSTableView) -> Int {
        return rowCount
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row >= 0, row < rows.count else {
            return nil
        }
        return rows[row]
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard let table = notification.object as? NSTableView else {
            return
        }
        selectRow(at: table.selectedRow)
    }
}

// MARK: - Label formatting helpers

/// Best-effort formatting helpers for custom-token labels shown next to a
/// settings row -- kept file-private since nothing outside this file needs
/// them.
private enum RowLabelFormat {
    static func captionKey(for token: String) -> String {
        let split = token.replacingOccurrences(of: "_", with: " ")
        return "label.\(split.lowercased())"
    }

    static func placeholderKey(for token: String) -> String {
        return "\(captionKey(for: token)).placeholder"
    }

    /// A computed summary combining both keys above -- exercises a
    /// computed var at file (not type-member) scope inside an enum body.
    static var supportedTokenCount: Int {
        return 4
    }
}

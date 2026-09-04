public class Container {
    interface Greeter {
        default String greet() {
            return "hi";
        }
    }

    enum Color {
        RED, GREEN;

        // Human-readable label for this color.
        String label() {
            return "color";
        }
    }
}

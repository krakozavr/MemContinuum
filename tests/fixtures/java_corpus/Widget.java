public class Widget {
    private int x;

    public Widget(int x) {
        this.x = x;
    }

    public int getValue() {
        return x;
    }

    interface Greeter {
        String greet();
    }

    abstract class Base {
        abstract void act();
    }

    record Point(int x, int y) {
        Point {
            if (x < 0) throw new IllegalArgumentException();
        }
    }
}

<?php
interface Greeter {
    public function greet();
}

abstract class Shape {
    abstract public function area();

    public function describe() {
        return "shape";
    }
}

enum Suit {
    case Hearts;
    case Spades;

    public function label() {
        return "suit";
    }
}

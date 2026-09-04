<?php

namespace Storage {
    class Box {
        public function open() {
            return 1;
        }
    }
}

namespace Archive {
    class Box {
        public function open() {
            return 2;
        }
    }
}

#pragma once
#include "reading.h"

namespace Storage {
    bool begin();
    bool enqueue(const Reading& reading);
    bool hasPending();
    bool peek(Reading& reading);
    bool removeFirst();
    bool clearQueue();
}
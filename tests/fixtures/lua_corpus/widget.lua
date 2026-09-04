function plain(a)
  return a + 1
end

local function loc(a)
  return a + 2
end

M.f = function(a)
  return a
end

function M.g(a)
  return a
end

function obj:m(a)
  return a
end

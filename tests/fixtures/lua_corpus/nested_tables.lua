function App.Services.load()
  return 1
end

function App.Services:reload()
  return 2
end

App.Services.save = function()
  return 3
end

function M.single()
  return 4
end

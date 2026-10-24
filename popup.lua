local request = ...
local function complete(state)
  return state.code ~= nil and not state.cleanup_error
    and not (state.win and vim.api.nvim_win_is_valid(state.win))
    and not (state.buf and vim.api.nvim_buf_is_valid(state.buf))
end
local previous = _G.dotfiles_rex_popup
if request.action ~= "open" then
  if not previous or previous.token ~= request.token then return 130 end
  if request.action == "close" or previous.code ~= nil then previous.finish(previous.code or 130) end
  if complete(previous) then return previous.code end
  if (previous.attempts or 0) >= 10 then error("Rex picker cleanup failed: " .. tostring(previous.cleanup_error)) end
  return -1
end
if previous and previous.code ~= nil and not complete(previous) then previous.finish(previous.code) end
assert(not previous or complete(previous), "A Rex picker is already open in this editor")
local mode = vim.fn.mode()
assert(mode == "n" or mode == "i" or mode == "t", "Leave the current editor mode before opening a Rex picker")
local original = vim.api.nvim_get_current_win()
local state = { token = request.token }
_G.dotfiles_rex_popup = state
local group = vim.api.nvim_create_augroup("dotfiles_rex_popup_" .. request.token, { clear = true })
local function startinsert(win)
  -- Typeahead runs after stopinsert unwinds; scheduled callbacks can run in the old input loop.
  local command = ("<Cmd>lua local p=_G.dotfiles_rex_popup; if p and p.token==%q and vim.api.nvim_get_current_win()==%d then vim.cmd.startinsert() end<CR>")
    :format(state.token, win)
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes(command, true, false, true), "ni", false)
end

function state.finish(code)
  if _G.dotfiles_rex_popup ~= state or state.finishing or complete(state) then return end
  if state.code == nil then
    state.code = code
    state.restore = state.win and vim.api.nvim_get_current_win() == state.win
  end
  state.finishing = true
  state.attempts = (state.attempts or 0) + 1
  local current = vim.api.nvim_get_current_win()
  if state.restore and current ~= state.win and current ~= original then state.restore = false end
  local errors = {}
  local function attempt(fn, ...)
    local ok, err = pcall(fn, ...)
    if not ok then errors[#errors + 1] = tostring(err) end
    return ok
  end
  if group and attempt(vim.api.nvim_del_augroup_by_id, group) then group = nil end
  if state.job and state.job > 0 then pcall(vim.fn.jobstop, state.job) end
  if state.win and vim.api.nvim_win_is_valid(state.win) then
    attempt(vim.api.nvim_win_close, state.win, true)
  end
  if state.buf and vim.api.nvim_buf_is_valid(state.buf) then
    attempt(vim.api.nvim_buf_delete, state.buf, { force = true })
  end
  if state.restore and vim.api.nvim_win_is_valid(original) then
    if attempt(function()
      vim.cmd.stopinsert()
      vim.api.nvim_set_current_win(original)
      if mode == "t" or mode == "i" then startinsert(original) end
    end) then state.restore = false end
  end
  if (state.win and vim.api.nvim_win_is_valid(state.win)) or (state.buf and vim.api.nvim_buf_is_valid(state.buf)) then
    errors[#errors + 1] = "popup window or buffer is still open"
  end
  state.cleanup_error = #errors > 0 and table.concat(errors, "; ") or nil
  state.finishing = false
end

local function bounds()
  local rows = math.max(1, vim.o.lines - vim.o.cmdheight - 1)
  return {
    relative = "editor", style = "minimal", zindex = 200,
    width = math.max(1, math.floor(vim.o.columns * 0.8)), height = math.max(1, math.floor(rows * 0.7)),
    col = math.floor(vim.o.columns * 0.1), row = math.floor(rows * 0.15),
  }
end

local ok, err = pcall(function()
  vim.cmd.stopinsert()
  state.buf = vim.api.nvim_create_buf(false, true)
  vim.bo[state.buf].bufhidden = "wipe"
  state.win = vim.api.nvim_open_win(state.buf, true, bounds())
  -- Default terminal color supplies glass; zero blending masks lower editor glyphs.
  vim.api.nvim_set_hl(0, "DotfilesRexPopup", { bg = "NONE" })
  vim.wo[state.win].winblend = 0
  vim.wo[state.win].winhighlight = "Normal:DotfilesRexPopup,NormalFloat:DotfilesRexPopup,EndOfBuffer:DotfilesRexPopup"
  vim.api.nvim_create_autocmd("BufWipeout", {
    -- Window/buffer changes inside BufWipeout can be locked until the event unwinds.
    group = group, buffer = state.buf, callback = function() vim.schedule(function() state.finish(130) end) end,
  })
  vim.api.nvim_create_autocmd("VimResized", {
    group = group, callback = function()
      if vim.api.nvim_win_is_valid(state.win) then vim.api.nvim_win_set_config(state.win, bounds()) end
    end,
  })
  state.job = vim.fn.jobstart(request.argv, {
    term = true, cwd = request.cwd,
    on_exit = function(_, code) vim.schedule(function() state.finish(code) end) end,
  })
  assert(state.job > 0, "Could not start the Rex picker terminal")
  startinsert(state.win)
end)
if not ok then
  state.finish(130)
  error(err)
end
return -1

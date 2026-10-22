# Hyperliquid API-Abdeckung

Vertragsstand: 2026-10-02. Die Matrix beschreibt implementierte, typisierte Verträge; keine behauptete Live-Börsenabnahme.

Der eingefrorene Upstream-Snapshot bleibt vom lokalen Prüflauf getrennt: zusätzliche Remediation-Regressionen wurden am 2026-10-09 offline geprüft. Dieses Prüfdatum behauptet keine neuere Live-API oder ausgeführte Mainnet-Orders. Die öffentliche Info-Abdeckung und ihre Raw-Payloads bleiben unverändert; marktbezogene Fillreads und begrenzte Orderindizes sind interne Belegverträge. Reserved, signed, sent und nativ consumed sind verschiedene Nonce-Zustände; eine belegbar ungesendete Anfrage wird nicht als ausgeführte Börsenoperation behandelt. Unknown verlangt Abgleich, keinen Skip/NoOp.

Documented public HyperCore Info, Exchange, HIP-3/HIP-4 and WebSocket API; official Python SDK public operations

| Familie | Vertrag | Werkzeug/Handler | Prüfdatei |
|---|---|---|---|
| Info | `activeAssetData` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `allBorrowLendReserveStates` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `allMids` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `allPerpMetas` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `approvedBuilders` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `borrowLendReserveState` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `borrowLendUserState` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `candleSnapshot` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `clearinghouseState` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `delegations` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `delegatorHistory` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `delegatorRewards` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `delegatorSummary` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `extraAgents` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `frontendOpenOrders` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `fundingHistory` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `historicalOrders` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `l2Book` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `maxBuilderFee` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `meta` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `metaAndAssetCtxs` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `openOrders` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `orderStatus` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `outcomeDeployerLimits` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `outcomeMeta` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `outcomeTemplates` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `perpAnnotation` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `perpCategories` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `perpConciseAnnotations` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `perpDeployAuctionStatus` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `perpDexLimits` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `perpDexStatus` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `perpDexs` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `perpsAtOpenInterestCap` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `portfolio` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `predictedFundings` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `referral` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `settledOutcome` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `spotClearinghouseState` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `spotDeployState` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `spotMeta` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `spotMetaAndAssetCtxs` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `spotPairDeployAuctionStatus` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `subAccounts` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `tokenDetails` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userAbstraction` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userDexAbstraction` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userFees` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userFills` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userFillsByTime` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userFunding` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userNonFundingLedgerUpdates` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userRateLimit` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userRole` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userStarState` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userToMultiSigSigners` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userTwapSliceFills` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `userVaultEquities` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Info | `vaultDetails` | `hl_info` / validierter InfoRequest | `test_info_wire_contracts.py` / unabhängiger MCP-HTTP-Vektor |
| Exchange | `activateOutcomeDeployer` | `hl_action` | `test_native_actions.py` |
| Exchange | `agentEnableDexAbstraction` | `hl_exchange:agent_enable_dex_abstraction` | `test_sdk_actions.py` |
| Exchange | `agentSendAsset` | `hl_action` | `test_native_actions.py` |
| Exchange | `agentSetAbstraction` | `hl_exchange:agent_set_abstraction` | `test_sdk_actions.py` |
| Exchange | `approveAgent` | `hl_action` | `test_native_actions.py` |
| Exchange | `approveBuilderFee` | `hl_exchange:approve_builder_fee` | `test_sdk_actions.py` |
| Exchange | `authorizeAqav2Role` | `hl_action` | `test_native_actions.py` |
| Exchange | `batchModify` | `hl_action` | `test_native_actions.py` |
| Exchange | `cDeposit` | `hl_action` | `test_native_actions.py` |
| Exchange | `cWithdraw` | `hl_action` | `test_native_actions.py` |
| Exchange | `cancel` | `hl_action` | `test_native_actions.py` |
| Exchange | `cancelByCloid` | `hl_action` | `test_native_actions.py` |
| Exchange | `claimRewards` | `hl_action` | `test_native_actions.py` |
| Exchange | `hip3LiquidatorTransfer` | `hl_action` | `test_native_actions.py` |
| Exchange | `modify` | `hl_action` | `test_native_actions.py` |
| Exchange | `multiSig` | `hl_multisig` | `test_multisig.py` |
| Exchange | `noop` | `hl_exchange:noop` | `test_sdk_actions.py` |
| Exchange | `order` | `hl_action` | `test_native_actions.py` |
| Exchange | `outcomeDeploy` | `hl_action` | `test_native_actions.py` |
| Exchange | `perpDeploy` | `hl_action` | `test_native_actions.py` |
| Exchange | `reserveRequestWeight` | `hl_action` | `test_native_actions.py` |
| Exchange | `scheduleCancel` | `hl_exchange:schedule_cancel` | `test_sdk_actions.py` |
| Exchange | `sendAsset` | `hl_exchange:send_asset` | `test_sdk_actions.py` |
| Exchange | `sendToEvmWithData` | `hl_action` | `test_native_actions.py` |
| Exchange | `spotDeploy` | `hl_exchange:spot_deploy_register_token` | `test_sdk_actions.py` |
| Exchange | `spotSend` | `hl_exchange:spot_transfer` | `test_sdk_actions.py` |
| Exchange | `tokenDelegate` | `hl_exchange:token_delegate` | `test_sdk_actions.py` |
| Exchange | `trailingStop` | `hl_action` | `test_native_actions.py` |
| Exchange | `twapCancel` | `hl_action` | `test_native_actions.py` |
| Exchange | `twapOrder` | `hl_action` | `test_native_actions.py` |
| Exchange | `updateIsolatedMargin` | `hl_exchange:update_isolated_margin` | `test_sdk_actions.py` |
| Exchange | `updateLeverage` | `hl_exchange:update_leverage` | `test_sdk_actions.py` |
| Exchange | `usdClassTransfer` | `hl_exchange:usd_class_transfer` | `test_sdk_actions.py` |
| Exchange | `usdSend` | `hl_exchange:usd_transfer` | `test_sdk_actions.py` |
| Exchange | `userDexAbstraction` | `hl_exchange:user_dex_abstraction` | `test_sdk_actions.py` |
| Exchange | `userOutcome` | `hl_action` | `test_native_actions.py` |
| Exchange | `userSetAbstraction` | `hl_exchange:user_set_abstraction` | `test_sdk_actions.py` |
| Exchange | `validatorL1Stream` | `hl_action` | `test_native_actions.py` |
| Exchange | `vaultTransfer` | `hl_exchange:vault_usd_transfer` | `test_sdk_actions.py` |
| Exchange | `withdraw3` | `hl_exchange:withdraw_from_bridge` | `test_sdk_actions.py` |
| Python-SDK | `agent_enable_dex_abstraction` | `hl_exchange:agent_enable_dex_abstraction` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `agent_set_abstraction` | `hl_exchange:agent_set_abstraction` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `approve_agent` | `hl_action:approveAgent` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `approve_builder_fee` | `hl_exchange:approve_builder_fee` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `bulk_cancel` | `hl_exchange:bulk_cancel` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `bulk_cancel_by_cloid` | `hl_exchange:bulk_cancel_by_cloid` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `bulk_modify_orders_new` | `hl_exchange:bulk_modify_orders_new` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `bulk_orders` | `hl_exchange:bulk_orders` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `c_signer_jail_self` | `hl_exchange:c_signer_jail_self` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `c_signer_unjail_self` | `hl_exchange:c_signer_unjail_self` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `c_validator_change_profile` | `hl_exchange:c_validator_change_profile` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `c_validator_register` | `hl_exchange:c_validator_register` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `c_validator_unregister` | `hl_exchange:c_validator_unregister` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `cancel` | `hl_exchange:cancel` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `cancel_by_cloid` | `hl_exchange:cancel_by_cloid` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `convert_to_multi_sig_user` | `hl_exchange:convert_to_multi_sig_user` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `create_sub_account` | `hl_exchange:create_sub_account` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `gossip_priority_bid` | `hl_exchange:gossip_priority_bid` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `market_close` | `hl_exchange:market_close` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `market_open` | `hl_exchange:market_open` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `modify_order` | `hl_exchange:modify_order` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `multi_sig` | `hl_multisig` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `noop` | `hl_exchange:noop` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `order` | `hl_exchange:order` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `perp_deploy_register_asset` | `hl_exchange:perp_deploy_register_asset` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `perp_deploy_set_oracle` | `hl_exchange:perp_deploy_set_oracle` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `schedule_cancel` | `hl_exchange:schedule_cancel` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `send_asset` | `hl_exchange:send_asset` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `set_referrer` | `hl_exchange:set_referrer` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_enable_freeze_privilege` | `hl_exchange:spot_deploy_enable_freeze_privilege` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_enable_quote_token` | `hl_exchange:spot_deploy_enable_quote_token` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_freeze_user` | `hl_exchange:spot_deploy_freeze_user` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_genesis` | `hl_exchange:spot_deploy_genesis` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_register_hyperliquidity` | `hl_exchange:spot_deploy_register_hyperliquidity` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_register_spot` | `hl_exchange:spot_deploy_register_spot` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_register_token` | `hl_exchange:spot_deploy_register_token` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_revoke_freeze_privilege` | `hl_exchange:spot_deploy_revoke_freeze_privilege` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_set_deployer_trading_fee_share` | `hl_exchange:spot_deploy_set_deployer_trading_fee_share` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_deploy_user_genesis` | `hl_exchange:spot_deploy_user_genesis` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `spot_transfer` | `hl_exchange:spot_transfer` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `sub_account_spot_transfer` | `hl_exchange:sub_account_spot_transfer` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `sub_account_transfer` | `hl_exchange:sub_account_transfer` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `token_delegate` | `hl_exchange:token_delegate` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `update_isolated_margin` | `hl_exchange:update_isolated_margin` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `update_leverage` | `hl_exchange:update_leverage` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `usd_class_transfer` | `hl_exchange:usd_class_transfer` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `usd_transfer` | `hl_exchange:usd_transfer` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `use_big_blocks` | `hl_exchange:use_big_blocks` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `user_dex_abstraction` | `hl_exchange:user_dex_abstraction` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `user_set_abstraction` | `hl_exchange:user_set_abstraction` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `vault_usd_transfer` | `hl_exchange:vault_usd_transfer` | `test_sdk_actions.py` / Spezialvertrag |
| Python-SDK | `withdraw_from_bridge` | `hl_exchange:withdraw_from_bridge` | `test_sdk_actions.py` / Spezialvertrag |
| WebSocket | `activeAssetCtx` | `hl_stream` | `test_streams.py` |
| WebSocket | `activeAssetData` | `hl_stream` | `test_streams.py` |
| WebSocket | `allDexsAssetCtxs` | `hl_stream` | `test_streams.py` |
| WebSocket | `allDexsClearinghouseState` | `hl_stream` | `test_streams.py` |
| WebSocket | `allMids` | `hl_stream` | `test_streams.py` |
| WebSocket | `bbo` | `hl_stream` | `test_streams.py` |
| WebSocket | `candle` | `hl_stream` | `test_streams.py` |
| WebSocket | `clearinghouseState` | `hl_stream` | `test_streams.py` |
| WebSocket | `fastAssetCtxs` | `hl_stream` | `test_streams.py` |
| WebSocket | `l2Book` | `hl_stream` | `test_streams.py` |
| WebSocket | `notification` | `hl_stream` | `test_streams.py` |
| WebSocket | `openOrders` | `hl_stream` | `test_streams.py` |
| WebSocket | `orderUpdates` | `hl_stream` | `test_streams.py` |
| WebSocket | `outcomeMetaUpdates` | `hl_stream` | `test_streams.py` |
| WebSocket | `spotState` | `hl_stream` | `test_streams.py` |
| WebSocket | `trades` | `hl_stream` | `test_streams.py` |
| WebSocket | `twapStates` | `hl_stream` | `test_streams.py` |
| WebSocket | `userEvents` | `hl_stream` | `test_streams.py` |
| WebSocket | `userFills` | `hl_stream` | `test_streams.py` |
| WebSocket | `userFundings` | `hl_stream` | `test_streams.py` |
| WebSocket | `userNonFundingLedgerUpdates` | `hl_stream` | `test_streams.py` |
| WebSocket | `userTwapHistory` | `hl_stream` | `test_streams.py` |
| WebSocket | `userTwapSliceFills` | `hl_stream` | `test_streams.py` |
| WebSocket | `webData3` | `hl_stream` | `test_streams.py` |
| HIP-3 | `registerAsset` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `registerAsset2` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setOracle` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setFundingMultipliers` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setFundingInterestRates` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setFundingClamps` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `haltTrading` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `insertMarginTable` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setMarginTableIds` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setFeeRecipient` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setOpenInterestCaps` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setSubDeployers` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setMarginModes` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setDeployerFees` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `setPerpAnnotation` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `disableDex` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 | `star` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-4 | `registerStandaloneOutcomeFromTemplate` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-4 | `registerQuestionFromTemplate` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-4 | `registerAndAssociateNamedOutcomeFromTemplate` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-4 | `settleOutcome` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-4 | `settleQuestion2` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-4 | `setSubDeployers` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 Star | `modifyApproval` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 Star | `modifyBackstopLiquidatorApproval` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 Star | `setReduceOnly` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 Star | `cancel` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 Star | `cancelAll` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 Star | `order` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |
| HIP-3 Star | `sendAsset` | `hl_action` / benannte Variante | `test_deployer_contracts.py` |

Preis-/Mengenraster, Signerrollen, Netzwerk-, Deployer- und Testnet-Grenzen bleiben maßgeblich. Ein API-Wallet hat keine Masterwallet-Befugnisse.

WebSocket-Posts unterstützen Info, SDK-Aktionen und native Aktionen. Reconnects abonnieren Reads erneut, senden Writes aber niemals erneut.

Der Stream begrenzt standardmäßig 128 einzigartige gewünschte Abonnements, 4096 Ereignisse und 8 MiB tatsächliche UTF-8-Ereignisbytes im Ring. Doppelte Abonnements verbrauchen kein weiteres Budget; eine Überschreitung wird vor Änderung der gewünschten Auswahl abgewiesen. `hl_stream`-Reads begrenzen die vollständige native Antwort auf 600.000 UTF-8-Bytes und setzen den Cursor ausschließlich bis zum letzten ausgegebenen Ereignis fort. Die Konstruktorbudgets sind konfigurierbar; die Antwortgrenze darf nicht erhöht werden.

Count-/Byteverlust und einzelne übergroße Ereignisse zeigen eine Lücke mit notwendigem REST-Abgleich. Ein übergroßer Einzelwert wird als begrenzter Gapmarker mit derselben Sequenz sichtbar, damit das Lesen fortschreitet. Decode-, Transport- und Timeoutdiagnosen enthalten nur Kategorie, Zeitpunkt und Zähler; keine Payloads oder Credentialtexte. Fehler beim Decodieren verhindern spätere gültige Ereignisse nicht. Info- und Action-Postantworten werden nach ihrer erwarteten Art korreliert; ein Reconnect bestätigt oder wiederholt keinen verlorenen Auftrag.

Die unabhängige Manifestdatei enthält Dokumentprüfsummen und Quellcommits. `--check-coverage` vergleicht diese erwarteten Verträge mit den vorhandenen Handlern und Eingabeschemas; Contracttests prüfen deren tatsächliche Wire-/Signingpfade.

Primärquellen: [Hyperliquid API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api), [HIP-3](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/hip-3-deployer-actions), [HIP-4](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/hip-4-deployer-actions), [Python-SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/tree/2fdb18f9517675ea03695a0962bd19eece9c83f0).

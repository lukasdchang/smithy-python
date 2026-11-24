/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
package software.amazon.smithy.python.aws.codegen;

import java.util.List;
import software.amazon.smithy.codegen.core.Symbol;
import software.amazon.smithy.codegen.core.SymbolReference;
import software.amazon.smithy.python.codegen.CodegenUtils;
import software.amazon.smithy.python.codegen.GenerationContext;
import software.amazon.smithy.python.codegen.SyncVariant;
import software.amazon.smithy.python.codegen.integrations.PythonIntegration;
import software.amazon.smithy.python.codegen.integrations.RuntimeClientPlugin;
import software.amazon.smithy.utils.SmithyInternalApi;

/**
 * Adds the recursion detection interceptor to AWS clients.
 *
 * <p>The interceptor propagates the Lambda invocation lineage in the
 * {@code X-Amzn-Trace-Id} header so that services can detect and halt runaway
 * workloads. It is added to every AWS client because the header is not modeled per
 * operation, and it passes requests through unchanged outside of Lambda.
 */
@SmithyInternalApi
public final class AwsRecursionDetectionIntegration implements PythonIntegration {

    private static final String PLUGIN_FILE = "recursion_detection";

    private static final String RECURSION_DETECTION_PLUGIN = """
            def recursion_detection_plugin(config: $1T) -> None:
                config.interceptors.append($2T())
            """;

    // Mode-neutral: the interceptor's hook is sync, so both client variants share it.
    private static final Symbol INTERCEPTOR = Symbol.builder()
            .namespace("smithy_aws_core.interceptors.recursion_detection", ".")
            .name("RecursionDetectionInterceptor")
            .addDependency(AwsPythonDependency.SMITHY_AWS_CORE)
            .build();

    @Override
    public List<RuntimeClientPlugin> getClientPlugins(GenerationContext context) {
        if (!CodegenUtils.isAwsService(context) || !context.applicationProtocol().isHttpProtocol()) {
            return List.of();
        }

        final SymbolReference plugin = SymbolReference.builder()
                .symbol(CodegenUtils.aioSymbol(context.settings(), PLUGIN_FILE, "recursion_detection_plugin"))
                .build();

        return List.of(
                RuntimeClientPlugin.builder()
                        .pythonPlugin(plugin)
                        .writeAdditionalFiles((c) -> {
                            var filename = plugin.getSymbol().getDefinitionFile();
                            var asyncConfig = CodegenUtils.getAsyncConfigSymbol(c.settings(), c.model());
                            c.writerDelegator()
                                    .useFileWriter(filename,
                                            plugin.getSymbol().getNamespace(),
                                            writer -> writer.write(RECURSION_DETECTION_PLUGIN,
                                                    asyncConfig,
                                                    INTERCEPTOR));
                            var syncFile = SyncVariant.writeSyncTwin(c,
                                    plugin.getSymbol(),
                                    RECURSION_DETECTION_PLUGIN,
                                    asyncConfig,
                                    INTERCEPTOR);
                            return List.of(filename, syncFile);
                        })
                        .build());
    }
}
